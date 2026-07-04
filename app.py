import re
import os
import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import DictCursor
from urllib.parse import urlparse, urlunparse, quote, parse_qsl, urlencode


# ---------------------------------------------------------
# Configuração (Secrets / variáveis de ambiente)
# ---------------------------------------------------------
def _secret(name, default=None):
    """Lê um valor de os.environ ou de st.secrets, nessa ordem."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return default


DB_URL = _secret("SUPABASE_DB_URL") or _secret("DATABASE_URL")
ADMIN_PASSWORD = str(_secret("ADMIN_PASSWORD", "5075"))
DEFAULT_SUPABASE_REGION = _secret("SUPABASE_REGION", "us-west-2")


# ---------------------------------------------------------
# Conexão com o banco
#
# O Supabase desativou o IPv4 direto: o host direto
# (db.<ref>.supabase.co) só responde por IPv6, e o Streamlit Cloud
# normalmente só tem IPv4 -> a conexão direta falha.
#
# A solução é o Connection Pooler (Supavisor), que atende por IPv4. Em
# vez de exigir a troca manual da URL, o app converte automaticamente
# uma URL direta na URL do pooler e a tenta primeiro. Se o pooler
# responder "Tenant or user not found" (região errada), tentamos outras
# regiões automaticamente. A causa real de qualquer falha é classificada
# e mostrada ao usuário (sem expor a senha).
# ---------------------------------------------------------
_DIRECT_HOST_RE = re.compile(r"^db\.([a-z0-9]+)\.supabase\.co$", re.IGNORECASE)
_POOLER_HOST_RE = re.compile(r"^aws-\d+-[a-z0-9-]+\.pooler\.supabase\.com$", re.IGNORECASE)

# Regiões tentadas como fallback quando o pooler não reconhece o tenant.
_FALLBACK_REGIONS = [
    "us-west-2", "us-east-1", "us-east-2", "sa-east-1", "eu-west-1",
    "eu-central-1", "ap-southeast-1", "ap-southeast-2", "ap-south-1",
    "us-west-1", "eu-west-2", "ca-central-1",
]

_NETWORK_SIGNALS = (
    "Network is unreachable", "timeout expired", "could not translate host name",
    "Connection refused", "No route to host", "server closed the connection",
    "Connection timed out",
)
_TENANT_SIGNAL = "Tenant or user not found"
_PASSWORD_SIGNAL = "password authentication failed"

_CONNECT_TIMEOUT = 8

# Estado de diagnóstico e cache da última URL que funcionou.
_DIAG = {"attempts": [], "kind": None}
_WORKING = {"url": None}


def _extract_ref(url):
    """Extrai o project ref de uma URL direta (db.<ref>...) ou de pooler
    (usuário postgres.<ref>)."""
    try:
        parts = urlparse(url)
    except Exception:
        return None
    match = _DIRECT_HOST_RE.match(parts.hostname or "")
    if match:
        return match.group(1)
    user = parts.username or ""
    if user.startswith("postgres."):
        return user.split(".", 1)[1]
    return None


def _ensure_sslmode(url):
    """Garante sslmode=require (o Supabase exige SSL)."""
    try:
        parts = urlparse(url)
        query = dict(parse_qsl(parts.query))
        query.setdefault("sslmode", "require")
        return urlunparse((parts.scheme, parts.netloc, parts.path,
                           parts.params, urlencode(query), parts.fragment))
    except Exception:
        return url


def build_pooler_url(url, region, port=5432):
    """Constrói a URL do Connection Pooler (Supavisor / IPv4) a partir de
    qualquer URL do Supabase. Retorna None se não conseguir o ref."""
    parts = urlparse(url)
    ref = _extract_ref(url)
    if not ref:
        return None
    user = f"postgres.{ref}"
    password = parts.password or ""
    host = f"aws-0-{region}.pooler.supabase.com"
    netloc = f"{quote(user)}:{quote(password)}@{host}:{port}"
    query = dict(parse_qsl(parts.query))
    query.setdefault("sslmode", "require")
    return urlunparse((parts.scheme, netloc, parts.path or "/postgres",
                       "", urlencode(query), ""))


def _candidate_urls():
    """URLs a tentar, em ordem de preferência."""
    if not DB_URL:
        return []
    parts = urlparse(DB_URL)
    host = parts.hostname or ""
    urls = []

    def add(u):
        if u and u not in urls:
            urls.append(u)

    if _DIRECT_HOST_RE.match(host):
        # Direta -> prioriza o Session Pooler (IPv4), depois a direta e o Transaction Pooler.
        add(build_pooler_url(DB_URL, DEFAULT_SUPABASE_REGION, 5432))
        add(_ensure_sslmode(DB_URL))
        add(build_pooler_url(DB_URL, DEFAULT_SUPABASE_REGION, 6543))
    else:
        # Já é pooler ou outro host: usa como está (com SSL garantido).
        add(_ensure_sslmode(DB_URL))
    return urls


def _mask(url):
    """Representação da URL sem a senha, para diagnóstico seguro."""
    try:
        parts = urlparse(url)
        user = parts.username or ""
        return f"{parts.scheme}://{user}:***@{parts.hostname}:{parts.port or ''}{parts.path}"
    except Exception:
        return "url inválida"


def _first_line(exc):
    text = (str(exc).strip() or repr(exc))
    return text.splitlines()[0][:200] if text else repr(exc)


def get_db_connection():
    if not DB_URL:
        return None

    attempts = []
    candidates = _candidate_urls()
    if _WORKING["url"] and _WORKING["url"] in candidates:
        candidates = [_WORKING["url"]] + [u for u in candidates if u != _WORKING["url"]]

    is_supabase = _extract_ref(DB_URL) is not None

    for url in candidates:
        try:
            conn = psycopg2.connect(url, connect_timeout=_CONNECT_TIMEOUT)
            _WORKING["url"] = url
            _DIAG.update(attempts=attempts, kind=None)
            return conn
        except Exception as exc:  # noqa: BLE001 - tentamos o próximo candidato
            msg = _first_line(exc)
            attempts.append((_mask(url), msg))

            # Se o pooler não reconheceu o tenant, a região provavelmente está
            # errada -> tenta outras regiões (falha rápida pós-TCP).
            if is_supabase and _TENANT_SIGNAL in msg:
                for region in _FALLBACK_REGIONS:
                    if region == DEFAULT_SUPABASE_REGION:
                        continue
                    stop = False
                    for port in (5432, 6543):
                        alt = build_pooler_url(DB_URL, region, port)
                        if not alt or alt in [a for a, _ in attempts]:
                            continue
                        try:
                            conn = psycopg2.connect(alt, connect_timeout=_CONNECT_TIMEOUT)
                            _WORKING["url"] = alt
                            _DIAG.update(attempts=attempts, kind=None)
                            return conn
                        except Exception as exc2:  # noqa: BLE001
                            m2 = _first_line(exc2)
                            attempts.append((_mask(alt), m2))
                            if any(sig in m2 for sig in _NETWORK_SIGNALS):
                                stop = True  # rede bloqueada -> não adianta insistir
                                break
                    if stop:
                        break

    # Classifica a causa provável para uma mensagem útil.
    joined = " ".join(m for _, m in attempts)
    if _PASSWORD_SIGNAL in joined:
        kind = "password"
    elif _TENANT_SIGNAL in joined:
        kind = "tenant"
    elif any(sig in joined for sig in _NETWORK_SIGNALS):
        kind = "network"
    else:
        kind = "other"
    _DIAG.update(attempts=attempts, kind=kind)
    return None


def init_db():
    """Cria as tabelas se necessário. Retorna True se conectou e migrou."""
    conn = get_db_connection()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as c:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        phone TEXT,
                        score INTEGER DEFAULT 0
                    )
                ''')
                c.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS matches (
                        match_id TEXT PRIMARY KEY,
                        team_a TEXT NOT NULL,
                        team_b TEXT NOT NULL,
                        score_a INTEGER,
                        score_b INTEGER,
                        completed BOOLEAN DEFAULT FALSE,
                        bet_amount REAL DEFAULT 0.0
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS predictions (
                        user_id TEXT,
                        match_id TEXT,
                        score_a INTEGER NOT NULL,
                        score_b INTEGER NOT NULL,
                        paid BOOLEAN DEFAULT FALSE,
                        PRIMARY KEY (user_id, match_id),
                        FOREIGN KEY (user_id) REFERENCES users (user_id),
                        FOREIGN KEY (match_id) REFERENCES matches (match_id)
                    )
                ''')
        return True
    finally:
        conn.close()


def get_or_create_user(name, phone=""):
    # Simple ID generation based on name
    user_id = name.lower().replace(" ", "_")
    conn = get_db_connection()
    if not conn:
        return user_id
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('SELECT * FROM users WHERE user_id = %s', (user_id,))
        user = c.fetchone()

        if not user:
            c.execute('INSERT INTO users (user_id, name, phone, score) VALUES (%s, %s, %s, %s)',
                      (user_id, name, phone, 0))
        elif phone and not user["phone"]:
            c.execute('UPDATE users SET phone = %s WHERE user_id = %s', (phone, user_id))

        conn.commit()
    finally:
        conn.close()
    return user_id


def create_match(team_a, team_b, bet_amount=0.0):
    match_id = f"{team_a}_{team_b}".lower().replace(" ", "_")
    conn = get_db_connection()
    if not conn:
        return match_id
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('SELECT * FROM matches WHERE match_id = %s', (match_id,))
        if not c.fetchone():
            c.execute('''
                INSERT INTO matches (match_id, team_a, team_b, completed, bet_amount)
                VALUES (%s, %s, %s, %s, %s)
            ''', (match_id, team_a, team_b, False, float(bet_amount)))
        elif bet_amount > 0:
            # Permite ao admin definir/atualizar a aposta depois.
            c.execute('UPDATE matches SET bet_amount = %s WHERE match_id = %s',
                      (float(bet_amount), match_id))
        conn.commit()
    finally:
        conn.close()
    return match_id


def parse_prediction(message_text, phone="", paid=False):
    # Expected format: "User Name: Team A 2 x 1 Team B" or "User Name: TeamA 2x1 TeamB"
    pattern = r"^([^:]+):\s*(.+?)\s+(\d+)\s*[xX-]\s*(\d+)\s+(.+)$"
    match = re.match(pattern, message_text.strip())

    if not match:
        return False, "Formato de mensagem inválido. Use 'Nome: Time A 2 x 1 Time B'"

    user_name = match.group(1).strip()
    team_a = match.group(2).strip()
    score_a = int(match.group(3))
    score_b = int(match.group(4))
    team_b = match.group(5).strip()

    user_id = get_or_create_user(user_name, phone=phone)
    match_id = create_match(team_a, team_b)

    conn = get_db_connection()
    if not conn:
        return False, "Banco de dados indisponível."
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('''
            INSERT INTO predictions (user_id, match_id, score_a, score_b, paid)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(user_id, match_id) DO UPDATE SET
                score_a=excluded.score_a,
                score_b=excluded.score_b,
                paid=excluded.paid
        ''', (user_id, match_id, score_a, score_b, paid))
        conn.commit()
    finally:
        conn.close()

    return True, f"Palpite de {user_name} registrado com sucesso para {team_a} x {team_b}."


def calculate_score(predicted_a, predicted_b, actual_a, actual_b):
    # 3 pontos para placar exato, 1 para acertar o vencedor/empate, 0 caso contrário.
    if predicted_a == actual_a and predicted_b == actual_b:
        return 3

    predicted_diff = predicted_a - predicted_b
    actual_diff = actual_a - actual_b

    if (predicted_diff == 0 and actual_diff == 0) or \
       (predicted_diff > 0 and actual_diff > 0) or \
       (predicted_diff < 0 and actual_diff < 0):
        return 1

    return 0


def delete_match(match_id):
    conn = get_db_connection()
    if not conn:
        return
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('DELETE FROM predictions WHERE match_id = %s', (match_id,))
        c.execute('DELETE FROM matches WHERE match_id = %s', (match_id,))
        conn.commit()
    finally:
        conn.close()


def delete_prediction(user_id, match_id):
    conn = get_db_connection()
    if not conn:
        return
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('DELETE FROM predictions WHERE user_id = %s AND match_id = %s', (user_id, match_id))
        conn.commit()
    finally:
        conn.close()


def update_match_result(match_id, score_a, score_b):
    conn = get_db_connection()
    if not conn:
        return False
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('SELECT 1 FROM matches WHERE match_id = %s', (match_id,))
        if c.fetchone():
            c.execute('''
                UPDATE matches
                SET score_a = %s, score_b = %s, completed = TRUE
                WHERE match_id = %s
            ''', (score_a, score_b, match_id))
            conn.commit()
            return True
        return False
    finally:
        conn.close()


def calculate_prize_split(match_id):
    conn = get_db_connection()
    if not conn:
        return 0, 0, []
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('SELECT * FROM matches WHERE match_id = %s', (match_id,))
        match = c.fetchone()

        if not match or not match["completed"]:
            return 0, 0, []

        bet_amount = match["bet_amount"]
        c.execute('SELECT * FROM predictions WHERE match_id = %s', (match_id,))
        match_predictions = c.fetchall()

        total_pot = len(match_predictions) * bet_amount
        if total_pot == 0:
            return 0, 0, []

        winners = [
            pred["user_id"]
            for pred in match_predictions
            if calculate_score(pred["score_a"], pred["score_b"],
                               match["score_a"], match["score_b"]) == 3
        ]
    finally:
        conn.close()

    if not winners:
        return total_pot, 0, []

    prize_per_winner = total_pot / len(winners)
    return total_pot, prize_per_winner, winners


def generate_ranking():
    conn = get_db_connection()
    if not conn:
        return []
    try:
        c = conn.cursor(cursor_factory=DictCursor)

        c.execute('SELECT user_id, name, score FROM users')
        all_users = {row["user_id"]: dict(row) for row in c.fetchall()}
        for u in all_users.values():
            u["score"] = 0  # Recalcula do zero em memória

        c.execute('SELECT * FROM matches WHERE completed = TRUE')
        completed_matches = c.fetchall()
        for match in completed_matches:
            c.execute('SELECT * FROM predictions WHERE match_id = %s', (match["match_id"],))
            for pred in c.fetchall():
                points = calculate_score(pred["score_a"], pred["score_b"],
                                         match["score_a"], match["score_b"])
                if points > 0 and pred["user_id"] in all_users:
                    all_users[pred["user_id"]]["score"] += points
    finally:
        conn.close()

    ranking = sorted(all_users.values(), key=lambda x: x["score"], reverse=True)
    return [{"name": u["name"], "score": u["score"]} for u in ranking]


# ---------------------------------------------------------
# Mensagens de erro de conexão
# ---------------------------------------------------------
def _render_connection_error():
    """Mostra a mensagem correta conforme a causa classificada + diagnóstico."""
    kind = _DIAG.get("kind")

    if kind == "network":
        st.error("⚠️ **Erro de Conexão com o Supabase:** O aplicativo não conseguiu se conectar ao banco de dados (rede/IPv4).")
        st.warning("O Supabase desativou o IPv4 direto. Como o Streamlit Cloud normalmente só tem IPv4, use o **Connection Pooler** do Supabase na sua `DATABASE_URL`, em vez da URL direta.")
        st.info("Para corrigir:\n\n"
                "1. Supabase → **Project Settings** → **Database** → **Connection pooling**.\n"
                "2. Copie a string do **Session pooler** (host `aws-0-<regiao>.pooler.supabase.com`, porta `5432`).\n"
                "3. Cole em **App settings → Secrets** do Streamlit como `DATABASE_URL`, trocando `[YOUR-PASSWORD]` pela senha do banco.")
    elif kind == "password":
        st.error("⚠️ **Falha de autenticação:** a senha do banco na `DATABASE_URL` está incorreta.")
        st.info("Confira/redefina a senha em Supabase → **Project Settings → Database** (botão *Reset password*) e atualize o `DATABASE_URL` nos Secrets do Streamlit. Cuidado com caracteres especiais na senha (eles precisam ser URL-encoded).")
    elif kind == "tenant":
        st.error("⚠️ **Projeto/usuário não reconhecido pelo pooler** ('Tenant or user not found').")
        st.info("Verifique se o usuário é `postgres.<project_ref>` e se a **região** do pooler está correta. Tentei várias regiões automaticamente sem sucesso — confirme o `project_ref` e a região em Supabase → Database → Connection pooling.")
    else:
        st.error("⚠️ **Não foi possível conectar ao banco de dados.** Veja os detalhes no diagnóstico abaixo.")

    attempts = _DIAG.get("attempts", [])
    if attempts:
        with st.expander("🔎 Diagnóstico de conexão (a senha é ocultada)"):
            for target, err in attempts:
                st.markdown(f"- `{target}`\n\n  → {err}")


# ---------------------------------------------------------
# Interface Streamlit
# ---------------------------------------------------------
def render_dashboard():
    st.set_page_config(page_title="Bolão de Futebol", page_icon="⚽")
    st.title("⚽ Bolão de Futebol - Dashboard")

    st.sidebar.header("Administração")
    admin_pw = st.sidebar.text_input("Senha de Administrador", type="password")

    if not DB_URL:
        st.error("⚠️ **Erro de Configuração:** o aplicativo não encontrou a URL do banco de dados.")
        st.info("Configure a `DATABASE_URL` (ou `SUPABASE_DB_URL`) nos Secrets do Streamlit ou nas variáveis de ambiente. Use a URL do **Connection Pooler** do Supabase.")
        st.stop()

    if not init_db():
        st.error("Aplicativo em modo offline devido a erro de banco de dados.")
        _render_connection_error()
        st.stop()

    conn = get_db_connection()
    if not conn:
        st.error("Aplicativo em modo offline devido a erro de banco de dados.")
        _render_connection_error()
        st.stop()

    c = conn.cursor(cursor_factory=DictCursor)

    # Fetch matches
    c.execute('SELECT * FROM matches')
    all_matches = c.fetchall()
    matches_dict = {m["match_id"]: dict(m) for m in all_matches}
    active_matches = {mid: m for mid, m in matches_dict.items() if not m["completed"]}

    if admin_pw == ADMIN_PASSWORD:
        # Adicionar Partida e Aposta
        st.sidebar.subheader("Criar/Atualizar Partida")
        team_a_input = st.sidebar.text_input("Time A", "Brasil")
        team_b_input = st.sidebar.text_input("Time B", "Noruega")
        bet_val = st.sidebar.number_input("Valor da Aposta (R$)", min_value=0.0, value=10.0, step=1.0)

        if st.sidebar.button("Salvar Partida"):
            create_match(team_a_input, team_b_input, bet_amount=bet_val)
            st.sidebar.success(f"Partida {team_a_input} x {team_b_input} salva! (Aposta: R$ {bet_val})")
            st.rerun()

        # Finalizar Partida
        st.sidebar.subheader("Finalizar Partida")
        if matches_dict:
            match_to_finish = st.sidebar.selectbox("Selecione a partida para encerrar", options=list(matches_dict.keys()))
            if match_to_finish:
                score_a = st.sidebar.number_input(f"Gols {matches_dict[match_to_finish]['team_a']}", min_value=0, step=1)
                score_b = st.sidebar.number_input(f"Gols {matches_dict[match_to_finish]['team_b']}", min_value=0, step=1)
                if st.sidebar.button("Encerrar Partida e Calcular"):
                    update_match_result(match_to_finish, score_a, score_b)
                    st.sidebar.success("Partida encerrada!")
                    st.rerun()

        # Deletar Partida
        st.sidebar.subheader("Deletar Partida")
        if matches_dict:
            match_to_delete = st.sidebar.selectbox("Selecione a partida para deletar", options=list(matches_dict.keys()), key="del_match_select")
            if match_to_delete:
                if st.sidebar.button("Deletar Partida", type="primary"):
                    delete_match(match_to_delete)
                    st.sidebar.success("Partida e palpites associados deletados!")
                    st.rerun()

        # Deletar Palpite
        st.sidebar.subheader("Deletar Palpite")
        c.execute('SELECT p.user_id, p.match_id, u.name, m.team_a, m.team_b FROM predictions p JOIN users u ON p.user_id = u.user_id JOIN matches m ON p.match_id = m.match_id')
        preds_for_del = c.fetchall()
        if preds_for_del:
            pred_options = {f"{p['user_id']}_{p['match_id']}": p for p in preds_for_del}
            pred_to_delete_key = st.sidebar.selectbox(
                "Selecione o palpite para deletar",
                options=list(pred_options.keys()),
                format_func=lambda k: f"{pred_options[k]['name']} - {pred_options[k]['team_a']}x{pred_options[k]['team_b']}",
                key="del_pred_select"
            )
            if pred_to_delete_key:
                if st.sidebar.button("Deletar Palpite", type="primary"):
                    p = pred_options[pred_to_delete_key]
                    delete_prediction(p['user_id'], p['match_id'])
                    st.sidebar.success("Palpite deletado!")
                    st.rerun()
    else:
        st.sidebar.info("Área restrita. Insira a senha para liberar o painel.")

    # Formulário para Registrar Palpite
    st.header("📝 Registrar Palpite")

    if not active_matches:
        st.info("Não há partidas abertas para receber palpites no momento.")
    else:
        with st.form("prediction_form"):
            st.write("Insira seus dados para participar do bolão:")
            user_name = st.text_input("Seu Nome")
            user_phone = st.text_input("Seu Telefone (obrigatório, ex: 11999999999)")

            selected_match_id = st.selectbox(
                "Selecione a Partida",
                options=list(active_matches.keys()),
                format_func=lambda mid: f"{active_matches[mid]['team_a']} x {active_matches[mid]['team_b']}"
            )

            team_a_pred = active_matches[selected_match_id]['team_a'] if selected_match_id else ""
            team_b_pred = active_matches[selected_match_id]['team_b'] if selected_match_id else ""

            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Time A: {team_a_pred}**")
                score_a_pred = st.number_input("Gols Time A", min_value=0, step=1)
            with col2:
                st.write(f"**Time B: {team_b_pred}**")
                score_b_pred = st.number_input("Gols Time B", min_value=0, step=1)

            paid_checkbox = st.checkbox("Pagamento da aposta realizado?")

            submit_btn = st.form_submit_button("Enviar Palpite")

            if submit_btn:
                # Validação simples de telefone brasileiro (10 a 11 dígitos)
                phone_digits = re.sub(r'\D', '', user_phone)
                phone_pattern = r'^\d{10,11}$'

                if not user_name:
                    st.error("Por favor, preencha o seu nome!")
                elif not user_phone or not re.match(phone_pattern, phone_digits):
                    st.error("Por favor, informe um número de WhatsApp válido (com DDD, somente números).")
                elif not selected_match_id:
                    st.error("Nenhuma partida selecionada!")
                else:
                    fake_msg = f"{user_name}: {team_a_pred} {score_a_pred} x {score_b_pred} {team_b_pred}"
                    success, msg = parse_prediction(fake_msg, phone=user_phone, paid=paid_checkbox)

                    if success:
                        st.success(f"{msg} Entraremos em contato via {user_phone} se você ganhar!")
                    else:
                        st.error(msg)

    # Exibir Ranking e Palpites Ativos
    col_rank, col_preds = st.columns(2)

    with col_rank:
        st.header("🏆 Ranking Geral")
        ranking = generate_ranking()
        if ranking:
            st.dataframe(pd.DataFrame(ranking), use_container_width=True)
        else:
            st.info("Nenhum usuário no ranking ainda.")

    with col_preds:
        st.header("📊 Palpites Registrados")
        c.execute('SELECT * FROM predictions')
        preds_data = c.fetchall()
        if preds_data:
            st.dataframe(pd.DataFrame([dict(p) for p in preds_data]), use_container_width=True)
        else:
            st.info("Nenhum palpite registrado.")

    # Exibir Status dos Bolões (Pix)
    st.header("💰 Status dos Prêmios (Pix)")

    c.execute('SELECT user_id, name FROM users')
    all_users = {row["user_id"]: row["name"] for row in c.fetchall()}

    for mid, match in matches_dict.items():
        if match["completed"]:
            tot_pot, prize_per, winners = calculate_prize_split(mid)
            st.markdown(f"**{match['team_a']} {match['score_a']} x {match['score_b']} {match['team_b']}**")
            st.write(f"Pote Total: **R$ {tot_pot:.2f}** | Prêmio por Ganhador: **R$ {prize_per:.2f}**")
            if winners:
                winner_names = [all_users.get(w, w) for w in winners]
                st.write(f"Ganhadores: {', '.join(winner_names)}")
            else:
                st.write("Sem ganhadores exatos para esta partida.")
            st.divider()

    conn.close()


if __name__ == "__main__":
    render_dashboard()
