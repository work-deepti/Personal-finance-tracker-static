"""Vercel application adapter for the desktop tracker's routes and business logic."""
import contextvars
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.request

import psycopg
from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from psycopg.types.json import Jsonb

import tracker

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 4_200_000
COOKIE = "__Host-investment-session"
SESSION_SECONDS = 12 * 60 * 60
store = contextvars.ContextVar("portfolio_store")
fields = {tracker.DB: "holdings", tracker.SNAP: "history", tracker.CACHE2Y: "chart_cache"}
tracker.MAX_FILE = 3 * 1024 * 1024


def database():
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5,
                           sslmode="require", prepare_threshold=None)


class Portfolio:
    def __init__(self, connection, user, editing):
        self.connection = connection
        self.user = user
        suffix = " for update" if editing else ""
        if editing:
            connection.execute(
                "insert into tracker_private.portfolios (user_id) values (%s) on conflict do nothing",
                (user,),
            )
        row = connection.execute(
            "select holdings, history, chart_cache from tracker_private.portfolios where user_id = %s" + suffix,
            (user,),
        ).fetchone()
        self.data = dict(zip(fields.values(), row or ([], {}, {})))

    def write(self, field, value):
        # field comes exclusively from the fixed mapping above.
        self.connection.execute(
            f"update tracker_private.portfolios set {field} = %s where user_id = %s",
            (Jsonb(value), self.user),
        )
        self.data[field] = value


def read(path, default):
    return store.get().data.get(fields[path], default)


def write(path, data, indent=None):
    portfolio = store.get()
    if path == tracker.CACHE2Y:
        portfolio.connection.execute(
            "update tracker_private.portfolios set chart_cache = %s where user_id = %s and holdings = %s",
            (Jsonb(data), portfolio.user, Jsonb(portfolio.data["holdings"])),
        )
        return
    portfolio.write(fields[path], data)


tracker.read = read
tracker.write = write


class WebHandler(tracker.Handler):
    def __init__(self, path):
        self.path = path

    def allowed(self, write):
        # The Flask dispatcher authenticates and checks write origins.
        return True

    def mutate(self, operation):
        try:
            operation()
        except OverflowError as error:
            self.send(413, {"error": str(error)})
        except (ValueError, KeyError) as error:
            self.send(400, {"error": str(error)})

    def body(self):
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def send(self, code, body, ctype="application/json", filename=None):
        self.response = (Response(body, status=code, content_type=ctype)
                         if isinstance(body, bytes) else (jsonify(body), code))
        if filename:
            self.response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return self.response


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(connection):
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 100:
        return None
    row = connection.execute(
        "select user_id from tracker_private.sessions where token_hash = %s and expires_at > now()",
        (token_hash(token),),
    ).fetchone()
    return row[0] if row else None


def same_origin():
    return request.headers.get("Origin") == os.environ["APP_ORIGIN"].rstrip("/")


@app.after_request
def response_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Request is too large. Import files must be 3 MB or smaller."), 413


@app.errorhandler(psycopg.Error)
def storage_error(_):
    app.logger.error("Portfolio database operation failed")
    return jsonify(error="Portfolio storage is temporarily unavailable."), 503


@app.get("/signin")
def signin_page():
    return send_from_directory(tracker.HERE, "signin.html")


def verify_password(email, password):
    base = os.environ["SUPABASE_URL"].rstrip("/")
    auth_request = urllib.request.Request(
        base + "/auth/v1/token?grant_type=password",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"apikey": os.environ["SUPABASE_PUBLISHABLE_KEY"],
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(auth_request, timeout=8) as response:
        return json.load(response)["user"]["id"]


@app.post("/signin")
def signin():
    if not same_origin() or not request.is_json:
        return jsonify(error="Sign in from the tracker page."), 403
    data = request.get_json(silent=True)
    email = data.get("email", "") if isinstance(data, dict) else ""
    password = data.get("password", "") if isinstance(data, dict) else ""
    if not isinstance(email, str) or not isinstance(password, str) or not (0 < len(email) <= 254 and 0 < len(password) <= 1024):
        return jsonify(error="Enter your email address and password."), 400
    try:
        user = verify_password(email.strip(), password)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            return jsonify(error="Too many sign-in attempts. Try again later."), 429
        if error.code in (400, 401, 403):
            return jsonify(error="Sign-in failed. Check your email and password."), 401
        return jsonify(error="Sign-in is temporarily unavailable."), 503
    except (OSError, ValueError, KeyError):
        return jsonify(error="Sign-in is temporarily unavailable."), 503
    token = secrets.token_urlsafe(32)
    with database() as connection:
        connection.execute("delete from tracker_private.sessions where expires_at <= now()")
        previous = request.cookies.get(COOKIE)
        if previous:
            connection.execute("delete from tracker_private.sessions where token_hash = %s",
                               (token_hash(previous),))
        connection.execute(
            "insert into tracker_private.sessions (token_hash, user_id, expires_at)"
            " values (%s, %s, now() + %s * interval '1 second')",
            (token_hash(token), user, SESSION_SECONDS),
        )
    response = jsonify(ok=True)
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=True, samesite="Lax", path="/")
    return response


@app.post("/signout")
def signout():
    if not same_origin():
        return jsonify(error="Sign out from the tracker page."), 403
    token = request.cookies.get(COOKIE)
    if token:
        with database() as connection:
            connection.execute("delete from tracker_private.sessions where token_hash = %s",
                               (token_hash(token),))
    response = jsonify(ok=True)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
    return response


@app.get("/")
def index():
    with database() as connection:
        if not current_user(connection):
            return redirect("/signin")
    return send_from_directory(tracker.HERE, "index.html")


@app.get("/portfolio.js")
@app.get("/favicon.ico")
def assets():
    filename = "portfolio.js" if request.path == "/portfolio.js" else "tracker.ico"
    return send_from_directory(tracker.HERE, filename)


@app.route("/api/<path:route>", methods=["GET", "POST", "PUT", "DELETE"])
def api(route):
    method = "GET" if request.method == "HEAD" else request.method
    editing = method != "GET"
    if editing and (not same_origin() or not request.is_json):
        return jsonify(error="Request must come from the tracker page."), 403
    with database() as connection:
        user = current_user(connection)
        if not user:
            return jsonify(error="Sign in to continue."), 401
        if route == "quit":
            return jsonify(error="Use Sign out to leave the online tracker."), 404
        path = request.path
        if request.query_string:
            path += "?" + request.query_string.decode("latin-1")
        handler = WebHandler(path)
        portfolio = Portfolio(connection, user, editing and route != "import/preview")
        if not editing:
            connection.commit()
        context = store.set(portfolio)
        try:
            getattr(handler, "do_" + method)()
            response = app.make_response(handler.response)
            if response.status_code >= 400:
                connection.rollback()
            return response
        finally:
            store.reset(context)
