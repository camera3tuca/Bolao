import re
import streamlit as st
import pandas as pd

# Data structures to store our state
users = {}          # dict of user_id -> {"name": str, "score": int}
matches = {}        # dict of match_id -> {"team_a": str, "team_b": str, "score_a": int, "score_b": int, "completed": bool}
predictions = []    # list of dicts: {"user_id": str, "match_id": str, "score_a": int, "score_b": int}

def get_or_create_user(name):
    # Simple ID generation based on name
    user_id = name.lower().replace(" ", "_")
    if user_id not in users:
        users[user_id] = {"name": name, "score": 0}
    return user_id

def create_match(team_a, team_b, bet_amount=0.0):
    match_id = f"{team_a}_{team_b}".lower().replace(" ", "_")
    if match_id not in matches:
         matches[match_id] = {
             "team_a": team_a,
             "team_b": team_b,
             "score_a": None,
             "score_b": None,
             "completed": False,
             "bet_amount": float(bet_amount)
         }
    else:
        # If the match exists, we should update the bet amount to allow admins to set it later
        if bet_amount > 0:
            matches[match_id]["bet_amount"] = float(bet_amount)

    return match_id

def parse_prediction(message_text):
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

    user_id = get_or_create_user(user_name)
    match_id = create_match(team_a, team_b)

    # Store or update the prediction
    # Remove existing prediction for this user and match if it exists
    global predictions
    predictions = [p for p in predictions if not (p["user_id"] == user_id and p["match_id"] == match_id)]

    predictions.append({
        "user_id": user_id,
        "match_id": match_id,
        "score_a": score_a,
        "score_b": score_b
    })

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
    if match_id in matches:
        matches[match_id]["score_a"] = score_a
        matches[match_id]["score_b"] = score_b
        matches[match_id]["completed"] = True
        return True
    return False

def calculate_prize_split(match_id):
    if match_id not in matches or not matches[match_id]["completed"]:
        return 0, 0, []

    match = matches[match_id]
    bet_amount = match.get("bet_amount", 0.0)

    # Find all predictions for this match
    match_predictions = [p for p in predictions if p["match_id"] == match_id]

    # Total pot is number of participants * bet amount
    total_pot = len(match_predictions) * bet_amount

    if total_pot == 0:
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

    # Calculate split
    if not winners:
        # If no one wins, maybe pot accumulates or is returned. For now, no winners.
        return total_pot, 0, []

    prize_per_winner = total_pot / len(winners)
    return total_pot, prize_per_winner, winners

def generate_ranking():
    # Reset all users' scores
    for user_id in users:
        users[user_id]["score"] = 0

    # Recalculate based on completed matches and predictions
    for pred in predictions:
        match_id = pred["match_id"]
        match = matches.get(match_id)
        if match and match["completed"]:
            points = calculate_score(
                pred["score_a"], pred["score_b"],
                match["score_a"], match["score_b"]
            )
            users[pred["user_id"]]["score"] += points

    # Generate sorted ranking list
    ranking = list(users.values())
    ranking.sort(key=lambda x: x["score"], reverse=True)
    return ranking

# ---------------------------------------------------------
# Streamlit Interface
# ---------------------------------------------------------
def render_dashboard():
    st.set_page_config(page_title="Bolão de Futebol", page_icon="⚽")
    st.title("⚽ Bolão de Futebol - Dashboard")

    st.sidebar.header("Administração")
    admin_pw = st.sidebar.text_input("Senha de Administrador", type="password")

    if admin_pw == "5075":
        # Adicionar Partida e Aposta
        st.sidebar.subheader("Criar/Atualizar Partida")
        team_a_input = st.sidebar.text_input("Time A", "Brasil")
        team_b_input = st.sidebar.text_input("Time B", "Noruega")
        bet_val = st.sidebar.number_input("Valor da Aposta (R$)", min_value=0.0, value=10.0, step=1.0)

        if st.sidebar.button("Salvar Partida"):
            match_id = create_match(team_a_input, team_b_input, bet_amount=bet_val)
            st.sidebar.success(f"Partida {team_a_input} x {team_b_input} salva! (Aposta: R$ {bet_val})")

        # Finalizar Partida
        st.sidebar.subheader("Finalizar Partida")
        if matches:
            match_to_finish = st.sidebar.selectbox("Selecione a partida para encerrar", options=list(matches.keys()))
            if match_to_finish:
                score_a = st.sidebar.number_input(f"Gols {matches[match_to_finish]['team_a']}", min_value=0, step=1)
                score_b = st.sidebar.number_input(f"Gols {matches[match_to_finish]['team_b']}", min_value=0, step=1)
                if st.sidebar.button("Encerrar Partida e Calcular"):
                    update_match_result(match_to_finish, score_a, score_b)
                    st.sidebar.success("Partida encerrada!")
    else:
        st.sidebar.info("Área restrita. Insira a senha para liberar o painel.")

    # Formulário para Registrar Palpite
    st.header("📝 Registrar Palpite")

    active_matches = {k: v for k, v in matches.items() if not v["completed"]}

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
                format_func=lambda mid: f"{matches[mid]['team_a']} x {matches[mid]['team_b']}"
            )

            # The selectbox will populate these variables automatically for the user
            team_a_pred = matches[selected_match_id]['team_a'] if selected_match_id else ""
            team_b_pred = matches[selected_match_id]['team_b'] if selected_match_id else ""

            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Time A: {team_a_pred}**")
                score_a_pred = st.number_input("Gols Time A", min_value=0, step=1)
            with col2:
                st.write(f"**Time B: {team_b_pred}**")
                score_b_pred = st.number_input("Gols Time B", min_value=0, step=1)

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
                    success, msg = parse_prediction(fake_msg)

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
        if predictions:
            df_preds = pd.DataFrame(predictions)
            st.dataframe(df_preds, use_container_width=True)
        else:
            st.info("Nenhum palpite registrado.")

    # Exibir Status dos Bolões (Pix)
    st.header("💰 Status dos Prêmios (Pix)")
    for mid, match in matches.items():
        if match["completed"]:
            tot_pot, prize_per, winners = calculate_prize_split(mid)
            st.markdown(f"**{match['team_a']} {match['score_a']} x {match['score_b']} {match['team_b']}**")
            st.write(f"Pote Total: **R$ {tot_pot:.2f}** | Prêmio por Ganhador: **R$ {prize_per:.2f}**")
            if winners:
                # Converter user_ids para nomes reais do dicionário users
                winner_names = [users.get(w, {}).get("name", w) for w in winners]
                st.write(f"Ganhadores: {', '.join(winner_names)}")
            else:
                st.write("Sem ganhadores exatos para esta partida.")
            st.divider()

if __name__ == "__main__":
    # Quando rodar via Streamlit, precisamos garantir que o estado persista em memória
    # O Streamlit recarrega o script a cada interação, então dicts globais zeram.
    # Vamos amarrar o estado global ao st.session_state

    if "users" not in st.session_state:
        st.session_state.users = users
    if "matches" not in st.session_state:
        st.session_state.matches = matches
    if "predictions" not in st.session_state:
        st.session_state.predictions = predictions

    # Re-apontar as variáveis globais para a sessão
    users = st.session_state.users
    matches = st.session_state.matches
    predictions = st.session_state.predictions

    render_dashboard()
