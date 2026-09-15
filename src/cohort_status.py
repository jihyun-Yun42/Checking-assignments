from . import db_client

if __name__ == "__main__":
    cohort = db_client.get_active_cohort()
    print("true" if cohort else "false", end="")
