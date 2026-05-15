# Project Plan

## Overview

This document outlines the plan for the development of the Python project. The project uses Poetry for dependency management and packaging. Below are the key steps and milestones for the project.

---

## 1. Project Setup

1. **Initialize Poetry Project**
   - Run `poetry init` to create a new Poetry project.
   - Project name: s3-timemachine
   - Define the project name, version, description, authors, license, and dependencies during the initialization process.

2. **Configure Project Structure**
   - Create the following directory structure:
     ```
     s3-timemachine/
     ├── s3_timemachine/
     │   ├── __init__.py
     │   ├── main.py
     │   └── utils.py
     ├── tests/
     │   ├── __init__.py
     │   ├── test_main.py
     │   └── test_utils.py
     ├── pyproject.toml
     ├── README.md
     ├── .gitignore
     ```
3. **Implement functionality**
   - A source bucket has tags attached to it, for example:
     - Key: `LockTime2026-04-16T19:07:35.427621+00:00`
       Value: `Locked until 2026-05-16T19:07:35.427621+00:00`
     - Key: `LockTime2026-04-19T19:07:35.098821+00:00`
       Value: `Locked until 2026-05-19T19:07:35.098821+00:00`
     - ...
   - Parse the tags to find all lock times which are locked until a future date (i.e., not expired)
   - Offer all time stamps to the user (lock time, not lock-until time), allowing them to select a point in time to restore the object to the destination bucket.
   - Go through the entire bucket recursively and determine all object versions that have been current at the time the user selects. This requires to take into account all current versions, as well as non-current versions and delete markers.
   - For each object version, get the storage class. If the storage class is glacier deep archive, determine if it has currently been resotred. If not, initiate a restore request with tier "Standard" or "Bulk" (configurable) and a configurable retention period.
   - If not all object versions are restored or are already available, print the object versions which we have to wait for and exit.
   - If all object versions are restored or are already available, copy the object versions to the destination bucket.
   - Add logging which defaults to "info" and has a --debug cli switch.
