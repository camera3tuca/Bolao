import app
import os

import psycopg2
from psycopg2.extras import DictCursor
import streamlit as st

def clear_test_data():
    try:
        conn = app.get_db_connection()
        if not conn: return
        c = conn.cursor(cursor_factory=DictCursor)
        # Delete only the specific test users and matches instead of dropping the entire table
        test_matches = ['brasil_argentina', 'brasil_noruega']
        for mid in test_matches:
            c.execute('DELETE FROM predictions WHERE match_id = %s', (mid,))
            c.execute('DELETE FROM matches WHERE match_id = %s', (mid,))

        test_users = ['joão', 'maria', 'pedro', 'ana']
        for uid in test_users:
            c.execute('DELETE FROM users WHERE user_id = %s', (uid,))

        conn.commit()
        conn.close()
    except Exception as e:
        print("Could not clean test data:", e)

def run_tests():
    # Attempting to initialize DB
    try:
        success = app.init_db()
        if success is False:
             print("Skipping tests, DB unconfigured.")
             return
        clear_test_data()
    except Exception as e:
        # In CI without db it should just stop
        print(f"Skipping tests, DB connection unavailable: {e}")
        return

    try:
        conn = app.get_db_connection()
        if not conn:
            print("Skipping tests, DB connection unavailable")
            return
        conn.close()
    except Exception:
        print("Skipping tests, DB connection unavailable")
        return

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
    conn = app.get_db_connection()
    c = conn.cursor(cursor_factory=DictCursor)
    c.execute('SELECT * FROM matches WHERE match_id=%s', (match_id,))
    assert c.fetchone() is not None

    # Check user creation and predictions
    c.execute("SELECT * FROM users WHERE user_id IN ('joão', 'maria', 'pedro', 'ana')")
    assert len(c.fetchall()) == 4
    c.execute("SELECT * FROM predictions WHERE match_id = %s", (match_id,))
    assert len(c.fetchall()) == 4
    conn.close()

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
