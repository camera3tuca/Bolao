import re
import os
from datetime import datetime, timezone, timedelta
import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import DictCursor
from urllib.parse import urlparse, urlunparse, quote, parse_qsl, urlencode

# Fuso de Brasília (BRT, UTC-3). O Brasil não usa mais horário de verão
# desde 2019, então o offset fixo é correto o ano todo.
BRT = timezone(timedelta(hours=-3))


def format_brt(dt):
    """Formata um datetime (timestamptz) no horário de Brasília. None -> '—'."""
    if dt is None:
        return "—"
    try:
        if isinstance(dt, str):
            return dt
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BRT).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return "—"


# set_page_config PRECISA ser o primeiro comando Streamlit do script (antes
# de qualquer acesso a st.secrets abaixo).
st.set_page_config(page_title="Bolão de Futebol", page_icon="⚽",
                   layout="wide", initial_sidebar_state="collapsed")


# ---------------------------------------------------------
# Configuração (Secrets / variáveis de ambiente)
# ---------------------------------------------------------
_SECRETS_CACHE = None


def _secrets_dict():
    """Carrega st.secrets uma única vez; retorna {} se não houver arquivo."""
    global _SECRETS_CACHE
    if _SECRETS_CACHE is None:
        try:
            _SECRETS_CACHE = dict(st.secrets)
        except Exception:
            _SECRETS_CACHE = {}
    return _SECRETS_CACHE


def _secret(name, default=None):
    """Lê um valor de os.environ ou de st.secrets, nessa ordem."""
    value = os.environ.get(name)
    if value:
        return value
    return _secrets_dict().get(name, default)


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
# Sinais transitórios (ex.: cold start do Neon no plano free) -> vale retentar.
_TRANSIENT_SIGNALS = (
    "timeout expired", "Connection timed out", "Connection reset",
    "server closed the connection", "could not receive data",
    "the database system is starting up", "Connection refused",
)

_CONNECT_TIMEOUT = 10
_MAX_ATTEMPTS_PER_URL = 2  # cold start do Neon pode falhar na 1ª tentativa

# Estado de diagnóstico e cache da última URL que funcionou.
_DIAG = {"attempts": [], "kind": None}
_WORKING = {"url": None}


def _provider():
    """Identifica o provedor do banco pela URL, para mensagens específicas."""
    url = (DB_URL or "").lower()
    if "neon.tech" in url:
        return "neon"
    if "supabase" in url or "pooler.supabase.com" in url:
        return "supabase"
    return "generic"


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


def _connect(url):
    """Conecta com pequena retentativa para erros transitórios (ex.: cold
    start do Neon no plano free). Levanta a última exceção se falhar."""
    import time
    last = None
    for attempt in range(_MAX_ATTEMPTS_PER_URL):
        try:
            # client_encoding=utf8 garante acentos (nomes com ã, ç, ...)
            # independentemente do locale do servidor.
            return psycopg2.connect(url, connect_timeout=_CONNECT_TIMEOUT,
                                    client_encoding="utf8")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < _MAX_ATTEMPTS_PER_URL and \
               any(sig in _first_line(exc) for sig in _TRANSIENT_SIGNALS):
                time.sleep(2)
                continue
            raise
    raise last


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
            conn = _connect(url)
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
                            conn = _connect(alt)
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
                # Data/hora do palpite. Palpites antigos ficam NULL (mostram
                # '—'); novos recebem now() via o DEFAULT abaixo.
                c.execute('ALTER TABLE predictions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;')
                c.execute('ALTER TABLE predictions ALTER COLUMN created_at SET DEFAULT now();')
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
            INSERT INTO predictions (user_id, match_id, score_a, score_b, paid, created_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT(user_id, match_id) DO UPDATE SET
                score_a=excluded.score_a,
                score_b=excluded.score_b,
                paid=excluded.paid,
                created_at=now()
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


def update_payment(user_id, match_id, paid):
    """Marca/desmarca a confirmação de pagamento de um palpite (admin)."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('UPDATE predictions SET paid = %s WHERE user_id = %s AND match_id = %s',
                  (bool(paid), user_id, match_id))
        conn.commit()
    finally:
        conn.close()


def get_predictions_detailed():
    """Palpites com dados do usuário e da partida, mais recentes primeiro."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        c = conn.cursor(cursor_factory=DictCursor)
        c.execute('''
            SELECT p.user_id, p.match_id, p.score_a, p.score_b, p.paid, p.created_at,
                   u.name, u.phone, m.team_a, m.team_b, m.completed, m.bet_amount
            FROM predictions p
            JOIN users u ON p.user_id = u.user_id
            JOIN matches m ON p.match_id = m.match_id
            ORDER BY p.created_at DESC NULLS LAST
        ''')
        return [dict(r) for r in c.fetchall()]
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

        bet_amount = match["bet_amount"] or 0.0
        c.execute('SELECT * FROM predictions WHERE match_id = %s', (match_id,))
        match_predictions = c.fetchall()

        # O pote é formado apenas pelos pagamentos CONFIRMADOS (dinheiro que
        # realmente entrou), não pelo número total de participantes.
        paid_count = sum(1 for p in match_predictions if p["paid"])
        total_pot = paid_count * bet_amount

        winners = [
            pred["user_id"]
            for pred in match_predictions
            if calculate_score(pred["score_a"], pred["score_b"],
                               match["score_a"], match["score_b"]) == 3
        ]
    finally:
        conn.close()

    if total_pot == 0 or not winners:
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

    provider = _provider()

    if kind == "network":
        st.error("⚠️ **Erro de Conexão:** o aplicativo não conseguiu se conectar ao banco de dados (rede/IPv4).")
        if provider == "neon":
            st.warning("Use o endpoint **com `-pooler`** do Neon (ele responde por IPv4) e mantenha `sslmode=require`. No plano free o Neon **suspende o compute** após inatividade, então a primeira conexão pode demorar alguns segundos — se acabou de acordar, recarregue a página.")
            st.info("No painel do Neon: **Connection Details** → ative **Connection pooling** → copie a *Connection string* (host `...-pooler.<regiao>.aws.neon.tech`) e cole em **App settings → Secrets** do Streamlit como `DATABASE_URL`.")
        else:
            st.warning("O Supabase desativou o IPv4 direto. Como o Streamlit Cloud normalmente só tem IPv4, use o **Connection Pooler** na sua `DATABASE_URL`, em vez da URL direta.")
            st.info("Para corrigir:\n\n"
                    "1. Supabase → **Project Settings** → **Database** → **Connection pooling**.\n"
                    "2. Copie a string do **Session pooler** (host `aws-0-<regiao>.pooler.supabase.com`, porta `5432`).\n"
                    "3. Cole em **App settings → Secrets** do Streamlit como `DATABASE_URL`, trocando `[YOUR-PASSWORD]` pela senha do banco.")
    elif kind == "password":
        st.error("⚠️ **Falha de autenticação:** a senha do banco na `DATABASE_URL` está incorreta.")
        if provider == "neon":
            st.info("Confira/redefina a senha no painel do Neon (**Connection Details → Reset password**) e atualize o `DATABASE_URL` nos Secrets do Streamlit. Cuidado com caracteres especiais na senha (precisam ser URL-encoded).")
        else:
            st.info("Confira/redefina a senha em Supabase → **Project Settings → Database** (botão *Reset password*) e atualize o `DATABASE_URL` nos Secrets do Streamlit. Cuidado com caracteres especiais na senha (precisam ser URL-encoded).")
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
def _inject_css():
    """Ajustes de cor/estilo (cards de indicadores, cabeçalho, botões)."""
    st.markdown(
        """
        <style>
          .block-container { padding-top: 2rem; max-width: 1050px; }
          h1 { color:#14532d; font-weight:800; letter-spacing:-.5px; }
          h2, h3 { color:#166534; }
          /* Cards de indicadores (st.metric) */
          div[data-testid="stMetric"]{
            background:#f0fdf4; border:1px solid #bbf7d0;
            border-left:5px solid #16a34a; padding:14px 16px;
            border-radius:12px; box-shadow:0 1px 2px rgba(0,0,0,.04);
          }
          div[data-testid="stMetricValue"]{ color:#15803d; font-weight:700; }
          div[data-testid="stMetricLabel"] p{ color:#374151; font-weight:600; }
          /* Expander do admin com destaque */
          details[open] > summary { color:#166534; }
          hr { margin:.8rem 0; border-color:#dcfce7; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _match_label(match):
    label = f"{match['team_a']} x {match['team_b']}"
    if match.get("completed"):
        label = f"✅ {label} ({match['score_a']} x {match['score_b']})"
    return label


def _render_admin_panel(matches_dict, detailed):
    """Conteúdo do painel de administração (dentro de um expander)."""
    st.markdown("**➕ Criar / Atualizar Partida**")
    ca, cb, cc = st.columns([2, 2, 1])
    team_a_input = ca.text_input("Time A", "Brasil", key="adm_ta")
    team_b_input = cb.text_input("Time B", "Noruega", key="adm_tb")
    bet_val = cc.number_input("Aposta (R$)", min_value=0.0, value=10.0, step=1.0, key="adm_bet")
    if st.button("💾 Salvar Partida", key="adm_save"):
        create_match(team_a_input, team_b_input, bet_amount=bet_val)
        st.success(f"Partida {team_a_input} x {team_b_input} salva! (Aposta: R$ {bet_val:.2f})")
        st.rerun()

    st.divider()
    st.markdown("**🏁 Finalizar Partida**")
    if matches_dict:
        m2f = st.selectbox("Partida para encerrar", list(matches_dict.keys()),
                           format_func=lambda mid: _match_label(matches_dict[mid]), key="adm_fin")
        if m2f:
            fa, fb = st.columns(2)
            sa = fa.number_input(f"Gols {matches_dict[m2f]['team_a']}", min_value=0, step=1, key="adm_sa")
            sb = fb.number_input(f"Gols {matches_dict[m2f]['team_b']}", min_value=0, step=1, key="adm_sb")
            if st.button("✅ Encerrar e Calcular", key="adm_finbtn"):
                update_match_result(m2f, sa, sb)
                st.success("Partida encerrada!")
                st.rerun()
    else:
        st.caption("Nenhuma partida cadastrada ainda.")

    st.divider()
    st.markdown("**💸 Confirmar Pagamento**")
    if detailed:
        pay_opts = {f"{d['user_id']}_{d['match_id']}": d for d in detailed}
        pk = st.selectbox("Palpite", list(pay_opts.keys()),
                          format_func=lambda k: f"{'✅' if pay_opts[k]['paid'] else '⏳'} {pay_opts[k]['name']} · {pay_opts[k]['team_a']}x{pay_opts[k]['team_b']}",
                          key="adm_pay")
        if pk:
            d = pay_opts[pk]
            if d["paid"]:
                if st.button("Marcar como PENDENTE ⏳", key="adm_payoff"):
                    update_payment(d["user_id"], d["match_id"], False)
                    st.rerun()
            else:
                if st.button("Marcar como PAGO ✅", key="adm_payon"):
                    update_payment(d["user_id"], d["match_id"], True)
                    st.rerun()
    else:
        st.caption("Nenhum palpite registrado ainda.")

    st.divider()
    st.markdown("**🗑️ Excluir**")
    dc1, dc2 = st.columns(2)
    with dc1:
        if matches_dict:
            m2d = st.selectbox("Partida", list(matches_dict.keys()),
                               format_func=lambda mid: _match_label(matches_dict[mid]), key="adm_delm")
            if m2d and st.button("Deletar Partida", type="primary", key="adm_delmbtn"):
                delete_match(m2d)
                st.success("Partida deletada!")
                st.rerun()
    with dc2:
        if detailed:
            del_opts = {f"{d['user_id']}_{d['match_id']}": d for d in detailed}
            kd = st.selectbox("Palpite", list(del_opts.keys()),
                              format_func=lambda k: f"{del_opts[k]['name']} · {del_opts[k]['team_a']}x{del_opts[k]['team_b']}",
                              key="adm_delp")
            if kd and st.button("Deletar Palpite", type="primary", key="adm_delpbtn"):
                d = del_opts[kd]
                delete_prediction(d["user_id"], d["match_id"])
                st.success("Palpite deletado!")
                st.rerun()


def render_dashboard():
    _inject_css()
    st.title("⚽ Bolão de Futebol - Dashboard")

    if not DB_URL:
        st.error("⚠️ **Erro de Configuração:** o aplicativo não encontrou a URL do banco de dados.")
        st.info("Configure a `DATABASE_URL` (ou `SUPABASE_DB_URL`) nos Secrets do Streamlit ou nas variáveis de ambiente. Use a string do **pooler** do seu provedor (Supabase Connection Pooler ou Neon endpoint `-pooler`).")
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
    c.execute('SELECT * FROM matches ORDER BY completed, team_a')
    matches_dict = {m["match_id"]: dict(m) for m in c.fetchall()}
    active_matches = {mid: m for mid, m in matches_dict.items() if not m["completed"]}
    detailed = get_predictions_detailed()

    # -----------------------------------------------------
    # Indicadores (KPIs) no topo
    # -----------------------------------------------------
    total_palpites = len(detailed)
    total_pagos = sum(1 for d in detailed if d["paid"])
    participantes = len({d["user_id"] for d in detailed})
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("⚽ Partidas", len(matches_dict))
    k2.metric("👥 Participantes", participantes)
    k3.metric("📝 Palpites", total_palpites)
    k4.metric("💸 Pagamentos", f"{total_pagos}/{total_palpites}" if total_palpites else "0/0")

    # -----------------------------------------------------
    # Administração: checkbox -> senha -> expander
    # -----------------------------------------------------
    if st.checkbox("🔐 Sou administrador"):
        admin_pw = st.text_input("Senha de Administrador", type="password", key="admin_pw")
        if admin_pw == ADMIN_PASSWORD:
            with st.expander("⚙️ Painel de Administração", expanded=True):
                _render_admin_panel(matches_dict, detailed)
        elif admin_pw:
            st.error("Senha incorreta.")

    st.divider()

    # -----------------------------------------------------
    # Registrar Palpite
    # -----------------------------------------------------
    st.subheader("📝 Registrar Palpite")
    if not active_matches:
        st.info("Não há partidas abertas para receber palpites no momento.")
    else:
        with st.form("prediction_form"):
            st.write("Insira seus dados para participar do bolão:")
            fc1, fc2 = st.columns(2)
            user_name = fc1.text_input("Seu Nome")
            user_phone = fc2.text_input("Seu WhatsApp (com DDD, ex: 11999999999)")

            selected_match_id = st.selectbox(
                "Selecione a Partida", options=list(active_matches.keys()),
                format_func=lambda mid: f"{active_matches[mid]['team_a']} x {active_matches[mid]['team_b']}")

            team_a_pred = active_matches[selected_match_id]['team_a'] if selected_match_id else ""
            team_b_pred = active_matches[selected_match_id]['team_b'] if selected_match_id else ""
            bet_amount = active_matches[selected_match_id]['bet_amount'] if selected_match_id else 0.0
            if bet_amount:
                st.caption(f"💰 Valor da aposta desta partida: **R$ {bet_amount:.2f}**")

            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**{team_a_pred}**")
                score_a_pred = st.number_input("Gols Time A", min_value=0, step=1)
            with col2:
                st.write(f"**{team_b_pred}**")
                score_b_pred = st.number_input("Gols Time B", min_value=0, step=1)

            paid_checkbox = st.checkbox("Já realizei o pagamento da aposta (Pix)")
            submit_btn = st.form_submit_button("Enviar Palpite", type="primary")

            if submit_btn:
                phone_digits = re.sub(r'\D', '', user_phone)
                if not user_name:
                    st.error("Por favor, preencha o seu nome!")
                elif not re.match(r'^\d{10,11}$', phone_digits):
                    st.error("Por favor, informe um número de WhatsApp válido (com DDD, somente números).")
                elif not selected_match_id:
                    st.error("Nenhuma partida selecionada!")
                else:
                    fake_msg = f"{user_name}: {team_a_pred} {score_a_pred} x {score_b_pred} {team_b_pred}"
                    success, msg = parse_prediction(fake_msg, phone=user_phone, paid=paid_checkbox)
                    if success:
                        registrado = format_brt(datetime.now(timezone.utc))
                        pgto = "✅ pagamento confirmado" if paid_checkbox else "⏳ pagamento pendente"
                        st.success(f"{msg}\n\n🕒 Registrado em {registrado} · {pgto}. "
                                   f"Entraremos em contato via {user_phone} se você ganhar!")
                        st.rerun()
                    else:
                        st.error(msg)

    st.divider()

    # -----------------------------------------------------
    # Palpites (logo abaixo da entrada de dados)
    # -----------------------------------------------------
    st.subheader("📊 Palpites Registrados")
    if detailed:
        filtro = st.radio("Filtrar por pagamento", ["Todos", "✅ Pagos", "⏳ Pendentes"], horizontal=True)
        rows = []
        for d in detailed:
            if filtro == "✅ Pagos" and not d["paid"]:
                continue
            if filtro == "⏳ Pendentes" and d["paid"]:
                continue
            rows.append({
                "Registrado em": format_brt(d["created_at"]),
                "Nome": d["name"],
                "Partida": f"{d['team_a']} x {d['team_b']}",
                "Palpite": f"{d['score_a']} x {d['score_b']}",
                "Pagamento": "✅ Pago" if d["paid"] else "⏳ Pendente",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum palpite para este filtro.")
    else:
        st.info("Nenhum palpite registrado.")

    st.divider()

    # -----------------------------------------------------
    # Ranking
    # -----------------------------------------------------
    st.subheader("🏆 Ranking Geral")
    ranking = generate_ranking()
    if ranking:
        df_rank = pd.DataFrame(ranking).rename(columns={"name": "Nome", "score": "Pontos"})
        df_rank.index = range(1, len(df_rank) + 1)
        df_rank.index.name = "Posição"
        st.dataframe(df_rank, use_container_width=True)
        st.caption("Pontuação: **3** pontos por placar exato · **1** por acertar o vencedor/empate.")
    else:
        st.info("Nenhum usuário no ranking ainda.")

    st.divider()

    # -----------------------------------------------------
    # Prêmios (Pix)
    # -----------------------------------------------------
    st.subheader("💰 Status dos Prêmios (Pix)")
    names_by_id = {d["user_id"]: d["name"] for d in detailed}
    matches_with_preds = [mid for mid in matches_dict if any(d["match_id"] == mid for d in detailed)]

    if not matches_with_preds:
        st.info("Nenhuma partida com palpites ainda.")
    for mid in matches_with_preds:
        match = matches_dict[mid]
        preds = [d for d in detailed if d["match_id"] == mid]
        n = len(preds)
        pagos = sum(1 for d in preds if d["paid"])
        bet = match["bet_amount"] or 0.0
        esperado = n * bet
        confirmado = pagos * bet

        st.markdown(f"### {match['team_a']} x {match['team_b']}")
        if not bet:
            st.caption("⚠️ Esta partida está sem valor de aposta. Defina em **Administração → Criar/Atualizar Partida**.")
        pc1, pc2, pc3, pc4 = st.columns(4)
        pc1.metric("Participantes", n)
        pc2.metric("Pote esperado", f"R$ {esperado:.2f}")
        pc3.metric("Confirmado (pago)", f"R$ {confirmado:.2f}")
        pc4.metric("Pendente", f"R$ {esperado - confirmado:.2f}",
                   delta=(f"{n - pagos} sem pagar" if n - pagos else "tudo pago"),
                   delta_color="inverse")

        if match["completed"]:
            tot_pot, prize_per, winners = calculate_prize_split(mid)
            st.markdown(f"**Resultado: {match['team_a']} {match['score_a']} x {match['score_b']} {match['team_b']}**")
            st.caption(f"Prêmio calculado sobre os **pagamentos confirmados**: R$ {tot_pot:.2f}.")
            if winners:
                winner_names = [names_by_id.get(w, w) for w in winners]
                st.success(f"🏆 Ganhador(es): {', '.join(winner_names)} — Prêmio: **R$ {prize_per:.2f}** cada")
            else:
                st.warning("Sem ganhadores de placar exato para esta partida.")
        else:
            st.caption("🔵 Partida em aberto — aguardando encerramento.")
        st.divider()

    conn.close()


if __name__ == "__main__":
    render_dashboard()
