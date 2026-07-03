import re
import streamlit as st
import pandas as pd
import os
import sqlite3

DB_FILE = "bolao.db"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
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
    try:
        c.execute('ALTER TABLE users ADD COLUMN phone TEXT')
    except sqlite3.OperationalError:
        pass # Column already exists

    # Create matches table
    c.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            match_id TEXT PRIMARY KEY,
            team_a TEXT NOT NULL,
            team_b TEXT NOT NULL,
            score_a INTEGER,
            score_b INTEGER,
            completed BOOLEAN DEFAULT 0,
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
            paid BOOLEAN DEFAULT 0,
            PRIMARY KEY (user_id, match_id),
            FOREIGN KEY (user_id) REFERENCES users (user_id),
            FOREIGN KEY (match_id) REFERENCES matches (match_id)
        )
    ''')

    conn.commit()
    conn.close()

# Initialize DB on app start
init_db()

def get_or_create_user(name, phone=""):
    # Simple ID generation based on name
    user_id = name.lower().replace(" ", "_")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = c.fetchone()

    if not user:
        c.execute('INSERT INTO users (user_id, name, phone, score) VALUES (?, ?, ?, ?)', (user_id, name, phone, 0))
    else:
        if phone and not user["phone"]:
            c.execute('UPDATE users SET phone = ? WHERE user_id = ?', (phone, user_id))

    conn.commit()
    conn.close()
    return user_id

def create_match(team_a, team_b, bet_amount=0.0):
    match_id = f"{team_a}_{team_b}".lower().replace(" ", "_")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM matches WHERE match_id = ?', (match_id,))
    if not c.fetchone():
         c.execute('''
            INSERT INTO matches (match_id, team_a, team_b, completed, bet_amount)
            VALUES (?, ?, ?, ?, ?)
         ''', (match_id, team_a, team_b, False, float(bet_amount)))
    else:
        # If the match exists, we should update the bet amount to allow admins to set it later
        if bet_amount > 0:
            c.execute('UPDATE matches SET bet_amount = ? WHERE match_id = ?', (float(bet_amount), match_id))

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
    c = conn.cursor()
    c.execute('''
        INSERT INTO predictions (user_id, match_id, score_a, score_b, paid)
        VALUES (?, ?, ?, ?, ?)
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

def update_match_result(match_id, score_a, score_b):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM matches WHERE match_id = ?', (match_id,))
    if c.fetchone():
        c.execute('''
            UPDATE matches
            SET score_a = ?, score_b = ?, completed = 1
            WHERE match_id = ?
        ''', (score_a, score_b, match_id))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def calculate_prize_split(match_id):
    conn = get_db_connection()
    c = conn.cursor()
    match = c.execute('SELECT * FROM matches WHERE match_id = ?', (match_id,)).fetchone()

    if not match or not match["completed"]:
        conn.close()
        return 0, 0, []

    bet_amount = match["bet_amount"]

    # Find all predictions for this match
    match_predictions = c.execute('SELECT * FROM predictions WHERE match_id = ?', (match_id,)).fetchall()

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
    c = conn.cursor()

    # Fetch all users initially to ensure everyone is in the ranking with at least 0
    all_users = {row["user_id"]: dict(row) for row in c.execute('SELECT user_id, name, score FROM users').fetchall()}
    for u in all_users.values():
        u["score"] = 0 # Calculate from scratch in-memory

    # Recalculate based on completed matches and predictions
    completed_matches = c.execute('SELECT * FROM matches WHERE completed = 1').fetchall()
    for match in completed_matches:
        preds = c.execute('SELECT * FROM predictions WHERE match_id = ?', (match["match_id"],)).fetchall()
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
    c = conn.cursor()

    # Fetch matches
    all_matches = c.execute('SELECT * FROM matches').fetchall()
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
            st.dataframe(df_ranking, width="stretch")
        else:
            st.info("Nenhum usuário no ranking ainda.")

    with col_preds:
        st.header("📊 Palpites Registrados")

        preds_data = c.execute('SELECT * FROM predictions').fetchall()
        if preds_data:
            df_preds = pd.DataFrame([dict(p) for p in preds_data])
            st.dataframe(df_preds, width="stretch")
        else:
            st.info("Nenhum palpite registrado.")

    # Exibir Status dos Bolões (Pix)
    st.header("💰 Status dos Prêmios (Pix)")

    # Pre-fetch all users to map IDs to names
    all_users = {row["user_id"]: row["name"] for row in c.execute('SELECT user_id, name FROM users').fetchall()}

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
