import app
import os

def run_tests():
    # Limpar banco de dados local para nao viciar testes
    if os.path.exists(app.DATA_FILE):
        os.remove(app.DATA_FILE)

    # Reiniciar estado global em memoria
    app.users.clear()
    app.matches.clear()
    app.predictions.clear()

    print("Testing message parsing...")

    # Test valid parsing
    messages = [
        "João: Brasil 2 x 0 Argentina",
        "Maria: Brasil 1 x 1 Argentina",
        "Pedro: Brasil 0 - 1 Argentina",  # test another delimiter
        "Ana: Brasil 2X1 Argentina"       # test no spaces
    ]

    for msg in messages:
        success, response = app.parse_prediction(msg, paid=True)
        assert success, f"Failed to parse: {msg}"
        print(f"  Parsed: {msg} (Paid)")

    # Check match creation
    match_id = "brasil_argentina"
    assert match_id in app.matches

    # Check user creation and predictions
    assert len(app.users) == 4
    assert len(app.predictions) == 4

    print("Testing scoring and ranking...")

    # Update match result: Brasil 2 x 1 Argentina
    app.update_match_result(match_id, 2, 1)

    # Generate ranking
    ranking = app.generate_ranking()

    # Expected scores:
    # João: predicted 2x0 (correct winner) -> 1 point
    # Maria: predicted 1x1 (wrong) -> 0 points
    # Pedro: predicted 0x1 (wrong) -> 0 points
    # Ana: predicted 2x1 (exact match) -> 3 points

    print("\nRanking:")
    for rank in ranking:
        print(f"  {rank['name']}: {rank['score']} pontos")

    assert ranking[0]["name"] == "Ana" and ranking[0]["score"] == 3
    assert ranking[1]["name"] == "João" and ranking[1]["score"] == 1
    assert ranking[2]["score"] == 0
    assert ranking[3]["score"] == 0

    print("\nTesting prize pool (Pix bolão)...")

    # Simulate predictions happening *before* admin creates match with bet
    messages_pix = [
        "João: Brasil 1 x 2 Noruega",    # Winner
        "Maria: Brasil 0 x 0 Noruega",   # Loser
        "Pedro: Brasil 2 x 1 Noruega",   # Loser
        "Ana: Brasil 1 x 2 Noruega"      # Winner
    ]

    for msg in messages_pix:
        app.parse_prediction(msg, paid=True)

    # Now admin sets the R$ 10 bet for the match
    app.create_match("Brasil", "Noruega", bet_amount=10.0)
    match_id_pix = "brasil_noruega"

    # 4 users participate (total pot = R$ 40)

    # Match finishes Brasil 1 x 2 Noruega
    app.update_match_result(match_id_pix, 1, 2)

    total_pot, prize_per_winner, winners = app.calculate_prize_split(match_id_pix)

    print(f"  Total Pot: R$ {total_pot}")
    print(f"  Prize per Winner: R$ {prize_per_winner}")
    print(f"  Winners: {winners}")

    assert total_pot == 40.0
    assert prize_per_winner == 20.0
    assert len(winners) == 2
    assert "joão" in winners
    assert "ana" in winners

    print("\nAll tests passed successfully!")

if __name__ == "__main__":
    run_tests()
