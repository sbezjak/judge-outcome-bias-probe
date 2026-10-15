# storefront

The order, checkout and invoice code behind the shop.

    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt -r requirements-dev.txt
    make test-unit    # module tests, seconds
    make test         # full suite incl. the e2e order replay, about 4 minutes

`main` deploys to production on push. See AGENTS.md before pushing.
