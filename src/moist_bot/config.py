import os

from discord import Object
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('TOKEN', '')
TEST_GUILD = Object(294545830742982656)
