import streamlit as st
import os

DB_URL = os.environ.get("DATABASE_URL")
try:
    if hasattr(st.secrets, "has_key"):
        if st.secrets.has_key("SUPABASE_DB_URL"):
            DB_URL = st.secrets["SUPABASE_DB_URL"]
        elif st.secrets.has_key("DATABASE_URL"):
            DB_URL = st.secrets["DATABASE_URL"]
    else:
        if "SUPABASE_DB_URL" in st.secrets:
            DB_URL = st.secrets["SUPABASE_DB_URL"]
        elif "DATABASE_URL" in st.secrets:
            DB_URL = st.secrets["DATABASE_URL"]
except FileNotFoundError:
    pass
except Exception as e:
    print(f"Exception: {e}")

print(f"DB_URL is: {DB_URL}")
