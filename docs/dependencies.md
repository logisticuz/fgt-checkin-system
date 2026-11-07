# Service dependency pins

`requirements.txt` records each service's top-level intent; Docker installs `requirements.lock`.
The locks capture the tested Python 3.11.10 environment, filtered to Linux CPython dependencies
and explicitly requested extras (including `psycopg[binary]`); test/dev extras are excluded.

To update deliberately:
1. Adjust the service's `requirements.txt` and resolve it in a disposable Python 3.11.10 test venv.
2. Install both services' intended requirements there, preserving explicit versions such as cryptography.
3. Run the application tests against the synthetic test database before accepting new versions.
4. From the repository root, regenerate both locks using that tested venv:
   `python -B -m tests.test_dependency_pins --write-locks`
5. Review the lock diff, then run `python -B -m pytest -p no:cacheprovider -q tests/test_dependency_pins.py`.
6. Have the conductor build both DEV images and smoke-test them before integrating the pins.

The generator reads installed metadata offline, checks version/Python constraints, and sorts the
full runtime dependency closure. It requires the test tools (pytest/packaging) in the venv.
It never upgrades packages or includes unrelated distributions from a full `pip freeze`.
Both images use `python:3.11.10-slim-bookworm`, matching the tested interpreter.
`nginx:1.27.5` fixes the last release in the task's specified 1.27 line; it is not a claim of current support.
Update image tags deliberately with the locks and repeat the DEV builds; other service images are deferred.
