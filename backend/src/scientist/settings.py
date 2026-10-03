import os


DATABASE_URL = os.environ.get(
    "SCIENTIST_DATABASE_URL",
    "postgresql+psycopg:///?dbname=scientist&host=/var/run/postgresql",
)
