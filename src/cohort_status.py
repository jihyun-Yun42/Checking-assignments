from . import sheets_client

if __name__ == "__main__":
    cohort = sheets_client.get_active_cohort()
    print("true" if cohort else "false", end="")
