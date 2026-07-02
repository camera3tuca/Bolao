import re

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
