import app

def run_tests():
    print("Testing message parsing...")

    # Test valid parsing
    messages = [
        "João: Brasil 2 x 0 Argentina",
        "Maria: Brasil 1 x 1 Argentina",
        "Pedro: Brasil 0 - 1 Argentina",  # test another delimiter
        "Ana: Brasil 2X1 Argentina"       # test no spaces
    ]

    for msg in messages:
        success, response = app.parse_prediction(msg)
        assert success, f"Failed to parse: {msg}"
        print(f"  Parsed: {msg}")

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

    print("\nAll tests passed successfully!")

if __name__ == "__main__":
    run_tests()
