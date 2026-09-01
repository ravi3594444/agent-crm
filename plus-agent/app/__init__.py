"""Load .env before anything reads os.environ."""
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(), override=False)