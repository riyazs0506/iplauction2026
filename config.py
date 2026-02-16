import os

is_production = os.environ.get("DB_HOST") is not None

if is_production:
    MYSQL_CONFIG = {
        "host": os.environ.get("DB_HOST"),
        "user": os.environ.get("DB_USER"),
        "password": os.environ.get("DB_PASSWORD"),
        "database": os.environ.get("DB_NAME"),
        "port": int(os.environ.get("DB_PORT", 3306)),
        "ssl_ca": "/etc/ssl/certs/ca-certificates.crt",
        "connection_timeout": 10,
        "autocommit": True
    }
else:
    MYSQL_CONFIG = {
        "host": "127.0.0.1",
        "user": "root",
        "password": "123456",
        "database": "ipl_auction_2026",
        "port": 3306,
        "connection_timeout": 10,
        "autocommit": True
    }
