import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
import psycopg2

load_dotenv(Path(__file__).with_name('.env'))

DATABASE_URL = os.getenv('DATABASE_URL')

@contextmanager
def get_connection() -> Iterator[psycopg2.extensions.connection]:
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL is not configured.')
    connection = psycopg2.connect(DATABASE_URL)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
