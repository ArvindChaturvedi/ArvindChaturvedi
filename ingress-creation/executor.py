#!/usr/local/bin/python

import subprocess
import os
from datetime import datetime
import time

def run_script(script_path):
    try:
        print("{} [Info]: Running script: {}".format(datetime.now(), script_path))
        result = subprocess.run(["python3", script_path], check=True)
        print("{} [Info]: Script finished successfully: {}".format(datetime.now(), script_path))
        return result
    except subprocess.CalledProcessError as e:
        print("{} [Error]: Script failed: {}".format(datetime.now(), script_path))
        print(e)
        return None
    except Exception as e:
        print("{} [Error]: Exception Occurred: {}".format(datetime.now(), e))
        print("{} [Error]: Exiting due to the above exception. Container must be restarted".format(datetime.now()))
        return None

if __name__ == "__main__":
    try:
        # Run the first script (gen.py)
        run_script("/app/gen.py")
        
        # Wait before running the next script
        time.sleep(10)

        # Run another Python script (second_script.py)
        run_script("/app/second_script.py")

    except Exception as e:
        print("{} [Error]: Exception Occurred in main: {}".format(datetime.now(), e))
        print("{} [Error]: Exiting".format(datetime.now()))
