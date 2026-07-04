import re
import streamlit as st
import pandas as pd
import os
import psycopg2
from psycopg2.extras import DictCursor
from urllib.parse import urlparse, urlunparse, quote

# Load database URL from Streamlit Secrets or environment variables
DB_URL = os.environ.get("DATABASE_URL")
try:
    if "SUPABASE_DB_URL" in st.secrets:
        DB_URL = st.secrets["SUPABASE_DB_URL"]
    elif "DATABASE_URL" in st.secrets:
        DB_URL = st.secrets["DATABASE_URL"]
except Exception:
    pass

# ---------------------------------------------------------
# Connection handling
#
# O Supabase desativou o IPv4 direto. O host direto
# (db.<ref>.supabase.co) só responde por IPv6, e o Streamlit Cloud
# normalmente só tem IPv4 -> por isso a conexão falha.
#
# A solução é usar o Connection Pooler (Supavisor), que atende por
# IPv4. Em vez de exigir que o usuário troque a URL manualmente, o app
# converte automaticamente uma URL direta na URL do pooler e tenta essa
# primeiro. A região pode ser informada via secret/env SUPABASE_REGION;
# caso contrário, usamos a região deste projeto como padrão.
# ---------------------------------------------------------

DEFAULT_SUPABASE_REGION = "us-west-2"
_DIRECT_HOST_RE = re.compile(r"^db\.([a-z0-9]+)\.supabase\.co$", re.IGNORECASE)


def _get_region():
    region = os.environ.get("SUPABASE_REGION")
    if region:
        return region
    try:
        if "SUPABASE_REGION" in st.secrets:
            return st.secrets["SUPABASE_REGION"]
    except Exception:
        pass
    return DEFAULT_SUPABASE_REGION


def build_pooler_url(url, port=5432):
    """Converte uma URL de conexão *direta* do Supabase para a URL do
    Connection Pooler (Supavisor), que responde por IPv4.

    Retorna None se a URL não for uma URL direta do Supabase."""
    try:
        parts = urlparse(url)
    except Exception:
        return None

    if not parts.hostname:
        return None

    match = _DIRECT_HOST_RE.match(parts.hostname)
    if not match:
        return None  # Não é uma URL direta do Supabase; nada a converter.

    ref = match.group(1)
    region = _get_region()
    user = f"postgres.{ref}"
    password = parts.password or ""
    host = f"aws-0-{region}.pooler.supabase.com"

    netloc = f"{quote(user)}:{quote(password)}@{host}:{port}"
    path = parts.path or "/postgres"
    return urlunparse((parts.scheme, netloc, path, "", parts.query, ""))


def _candidate_urls():
    """Lista ordenada de URLs a tentar. Para URLs diretas do Supabase,
    tentamos o Session Pooler (IPv4) primeiro, depois a direta, depois o
    Transaction Pooler."""
    if not DB_URL:
        return []

    parts = urlparse(DB_URL)
    is_direct = bool(parts.hostname and _DIRECT_HOST_RE.match(parts.hostname))

    urls = []
    if is_direct:
        for candidate in (
            build_pooler_url(DB_URL, 5432),  # Session pooler (recomendado p/ Streamlit)
            DB_URL,                          # Conexão direta (funciona onde há IPv6)
            build_pooler_url(DB_URL, 6543),  # Transaction pooler
        ):
            if candidate and candidate not in urls:
                urls.append(candidate)
    else:
        urls.append(DB_URL)
    return urls


# Guarda a URL que funcionou para evitar retentar todas a cada chamada.
_WORKING_URL = {"value": None}


def _render_connection_error():
    """Mostra a ajuda de conexão apenas uma vez por execução."""
    try:
        if st.session_state.get("_conn_error_shown"):
            return
        st.session_state["_conn_error_shown"] = True
    except Exception:
        pass

    st.error("⚠️ **Erro de Conexão com o Supabase:** O aplicativo não conseguiu se conectar ao banco de dados.")
    st.warning("O Supabase desativou o suporte direto a IPv4 recentemente. Como o Streamlit Cloud muitas vezes precisa de IPv4, você precisa usar o **Connection Pooler** do Supabase na sua `DATABASE_URL` em vez da URL direta.")
    st.info("Para corrigir isso:\n\n"
            "1. Vá no painel do Supabase do seu projeto.\n"
            "2. Clique em **Project Settings** (engrenagem) -> **Database**.\n"
            "3. Role para baixo até **Connection pooling**.\n"
            "4. Copie a string de conexão (ela costuma ter a porta `6543`/`5432` e usar o host `aws-0...pooler.supabase.com`).\n"
            "5. Troque a `DATABASE_URL` no Streamlit Secrets por essa nova URL do pooler, certificando-se de colocar sua senha onde estiver `[YOUR-PASSWORD]`.")


def get_db_connection():
    if not DB_URL:
        return None

    candidates = _candidate_urls()
    # Tenta primeiro a URL que já funcionou nesta sessão.
    if _WORKING_URL["value"] in candidates:
        candidates = [_WORKING_URL["value"]] + [u for u in candidates if u != _WORKING_URL["value"]]

    last_error = None
    for url in candidates:
        try:
            conn = psycopg2.connect(url, connect_timeout=10)
            _WORKING_URL["value"] = url
            return conn
        except Exception as e:  # noqa: BLE001 - tentamos o próximo candidato
            last_error = e
            continue

    # Todos os candidatos falharam.
    err_text = str(last_error) if last_error else ""
    network_signals = (
        "Network is unreachable",
        "could not translate host name",
        "connection to server at",
        "timeout expired",
        "Connection refused",
    )
    if any(sig in err_text for sig in network_signals):
        _render_connection_error()
    else:
        st.error(f"Erro ao conectar ao banco de dados: {last_error}")
    return None

@st.cache_resource
def init_db():
    if not DB_URL:
        return False
    conn = get_db_connection()
    if conn is None:
        return False
    c = conn.cursor()

    # Create users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT,
            score INTEGER DEFAULT 0
        )
    ''')

    # Ensure phone column exists for existing DBs
    c.execute('''
        ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
    ''')

    # Create matches table
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

    # Create predictions table
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

    conn.commit()
    conn.close()
    return True

# Initialize DB on app start
if not DB_URL:
    st.error("⚠️ **Erro de Configuração:** O aplicativo não encontrou a URL do banco de dados.")
    st.info("Configure a `DATABASE_URL` no `.streamlit/secrets.toml` ou nas variáveis de ambiente.")
else:
    init_db()

def get_or_create_user(name, phone=""):
    # Simple ID generation based on name
    user_id = name.lower().replace(" ", "_")
    conn = get_db_connection()
    if not conn: return user_id
    c = conn.cursor(cursor_factory=DictCursor)
    c.execute('SELECT * FROM users WHERE user_id = %s', (user_id,))
    user = c.fetchone()

    if not user:
        c.execute('INSERT INTO users (user_id, name, phone, score) VALUES (%s, %s, %s, %s)', (user_id, name, phone, 0))
    else:
        if phone and not user["phone"]:
            c.execute('UPDATE users SET phone = %s WHERE user_id = %s', (phone, user_id))

    conn.commit()
    conn.close()
    return user_id

def create_match(team_a, team_b, bet_amount=0.0):
    match_id = f"{team_a}_{team_b}".lower().replace(" ", "_")
    conn = get_db_connection()
    if not conn: return match_id
    c = conn.cursor(cursor_factory=DictCursor)
    c.execute('SELECT * FROM matches WHERE match_id = %s', (match_id,))
    if not c.fetchone():
         c.execute('''
            INSERT INTO matches (match_id, team_a, team_b, completed, bet_amount)
            VALUES (%s, %s, %s, %s, %s)
         ''', (match_id, team_a, team_b, False, float(bet_amount)))
    else:
        # If the match exists, we should update the bet amount to allow admins to set it later
        if bet_amount > 0:
            c.execute('UPDATE matches SET bet_amount = %s WHERE match_id = %s', (float(bet_amount), match_id))

    conn.commit()
    conn.close()
    return match_id

def parse_prediction(message_text, phone="", paid=False):
    # Expected format: "User Name: Team A 2 x 1 Team B" or "User Name: TeamA 2x1 TeamB"
    # We use a regex to capture:
    # Group 1: User name
    # Group 2: Team A
    # Group 3: Score A
    # Group 4: Score B
    # Group 5: Team B
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

    # Store or update the prediction
    conn = get_db_connection()
    if not conn: return False, "Banco de dados indisponível."
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
    conn.close()

    return True, f"Palpite de {user_name} registrado com sucesso para {team_a} x {team_b}."

def calculate_score(predicted_a, predicted_b, actual_a, actual_b):
    # Rule:
    # 3 points for exact score
    # 1 point for correct winner (or correct draw)
    # 0 points otherwise

    if predicted_a == actual_a and predicted_b == actual_b:
        return 3

    predicted_diff = predicted_a - predicted_b
    actual_diff = actual_a - actual_b

    # Check if both are draws, both are team_a wins, or both are team_b wins
    if (predicted_diff == 0 and actual_diff == 0) or \
       (predicted_diff > 0 and actual_diff > 0) or \
       (predicted_diff < 0 and actual_diff < 0):
        return 1

    return 0

def delete_match(match_id):
    conn = get_db_connection()
    if not conn: return
    c = conn.cursor(cursor_factory=DictCursor)
    c.execute('DELETE FROM predictions WHERE match_id = %s', (match_id,))
    c.execute('DELETE FROM matches WHERE match_id = %s', (match_id,))
    conn.commit()
    conn.close()

def delete_prediction(user_id, match_id):
    conn = get_db_connection()
    if not conn: return
    c = conn.cursor(cursor_factory=DictCursor)
    c.execute('DELETE FROM predictions WHERE user_id = %s AND match_id = %s', (user_id, match_id))
    conn.commit()
    conn.close()

def update_match_result(match_id, score_a, score_b):
    conn = get_db_connection()
    if not conn: return False
    c = conn.cursor(cursor_factory=DictCursor)
    c.execute('SELECT * FROM matches WHERE match_id = %s', (match_id,))
    if c.fetchone():
        c.execute('''
            UPDATE matches
            SET score_a = %s, score_b = %s, completed = TRUE
            WHERE match_id = %s
        ''', (score_a, score_b, match_id))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def calculate_prize_split(match_id):
    conn = get_db_connection()
    if not conn: return 0, 0, []
    c = conn.cursor(cursor_factory=DictCursor)
    c.execute('SELECT * FROM matches WHERE match_id = %s', (match_id,))
    match = c.fetchone()

    if not match or not match["completed"]:
        conn.close()
        return 0, 0, []

    bet_amount = match["bet_amount"]

    # Find all predictions for this match
    c.execute('SELECT * FROM predictions WHERE match_id = %s', (match_id,))
    match_predictions = c.fetchall()

    # Total pot is number of participants * bet amount
    total_pot = len(match_predictions) * bet_amount

    if total_pot == 0:
        conn.close()
        return 0, 0, []

    # Find winners (exact match score -> 3 points)
    winners = []
    for pred in match_predictions:
        points = calculate_score(
            pred["score_a"], pred["score_b"],
            match["score_a"], match["score_b"]
        )
        if points == 3:
            winners.append(pred["user_id"])

    conn.close()

    # Calculate split
    if not winners:
        # If no one wins, maybe pot accumulates or is returned. For now, no winners.
        return total_pot, 0, []

    prize_per_winner = total_pot / len(winners)
    return total_pot, prize_per_winner, winners

def generate_ranking():
    conn = get_db_connection()
    if not conn: return []
    c = conn.cursor(cursor_factory=DictCursor)

    # Fetch all users initially to ensure everyone is in the ranking with at least 0
    c.execute('SELECT user_id, name, score FROM users')
    all_users = {row["user_id"]: dict(row) for row in c.fetchall()}
    for u in all_users.values():
        u["score"] = 0 # Calculate from scratch in-memory

    # Recalculate based on completed matches and predictions
    c.execute('SELECT * FROM matches WHERE completed = TRUE')
    completed_matches = c.fetchall()
    for match in completed_matches:
        c.execute('SELECT * FROM predictions WHERE match_id = %s', (match["match_id"],))
        preds = c.fetchall()
        for pred in preds:
            points = calculate_score(
                pred["score_a"], pred["score_b"],
                match["score_a"], match["score_b"]
            )
            if points > 0 and pred["user_id"] in all_users:
                all_users[pred["user_id"]]["score"] += points

    # Sort the dictionary values to generate a ranking list
    ranking = list(all_users.values())
    ranking.sort(key=lambda x: x["score"], reverse=True)

    # Only return name and score for UI
    final_ranking = [{"name": u["name"], "score": u["score"]} for u in ranking]

    conn.close()
    return final_ranking

# ---------------------------------------------------------
# Streamlit Interface
# ---------------------------------------------------------
def render_dashboard():
    st.set_page_config(page_title="Bolão de Futebol", page_icon="⚽")
    st.title("⚽ Bolão de Futebol - Dashboard")

    st.sidebar.header("Administração")
    admin_pw = st.sidebar.text_input("Senha de Administrador", type="password")

    conn = get_db_connection()
    if not conn:
        st.error("Aplicativo em modo offline devido a erro de banco de dados. Configure sua DATABASE_URL no Streamlit Secrets de acordo com as instruções acima e reinicie.")
        st.stop()

    c = conn.cursor(cursor_factory=DictCursor)

    # Fetch matches
    c.execute('SELECT * FROM matches')
    all_matches = c.fetchall()
    matches_dict = {m["match_id"]: dict(m) for m in all_matches}
    active_matches = {mid: m for mid, m in matches_dict.items() if not m["completed"]}

    if admin_pw == "5075":
        # Adicionar Partida e Aposta
        st.sidebar.subheader("Criar/Atualizar Partida")
        team_a_input = st.sidebar.text_input("Time A", "Brasil")
        team_b_input = st.sidebar.text_input("Time B", "Noruega")
        bet_val = st.sidebar.number_input("Valor da Aposta (R$)", min_value=0.0, value=10.0, step=1.0)

        if st.sidebar.button("Salvar Partida"):
            match_id = create_match(team_a_input, team_b_input, bet_amount=bet_val)
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

            # The selectbox will populate these variables automatically for the user
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
                import re as regex_lib
                # Simple validation for Brazilian phone numbers (10 to 11 digits)
                phone_pattern = r'^\d{10,11}$'

                if not user_name:
                    st.error("Por favor, preencha o seu nome!")
                elif not user_phone or not regex_lib.match(phone_pattern, re.sub(r'\D', '', user_phone)):
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
            df_ranking = pd.DataFrame(ranking)
            st.dataframe(df_ranking, use_container_width=True)
        else:
            st.info("Nenhum usuário no ranking ainda.")

    with col_preds:
        st.header("📊 Palpites Registrados")

        c.execute('SELECT * FROM predictions')
        preds_data = c.fetchall()
        if preds_data:
            df_preds = pd.DataFrame([dict(p) for p in preds_data])
            st.dataframe(df_preds, use_container_width=True)
        else:
            st.info("Nenhum palpite registrado.")

    # Exibir Status dos Bolões (Pix)
    st.header("💰 Status dos Prêmios (Pix)")

    # Pre-fetch all users to map IDs to names
    c.execute('SELECT user_id, name FROM users')
    all_users = {row["user_id"]: row["name"] for row in c.fetchall()}

    for mid, match in matches_dict.items():
        if match["completed"]:
            tot_pot, prize_per, winners = calculate_prize_split(mid)
            st.markdown(f"**{match['team_a']} {match['score_a']} x {match['score_b']} {match['team_b']}**")
            st.write(f"Pote Total: **R$ {tot_pot:.2f}** | Prêmio por Ganhador: **R$ {prize_per:.2f}**")
            if winners:
                # Converter user_ids para nomes reais do banco de dados
                winner_names = [all_users.get(w, w) for w in winners]
                st.write(f"Ganhadores: {', '.join(winner_names)}")
            else:
                st.write("Sem ganhadores exatos para esta partida.")
            st.divider()

    conn.close()

if __name__ == "__main__":
    render_dashboard()
