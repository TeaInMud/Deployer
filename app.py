import os, zipfile, shutil, uuid, json, subprocess, signal, sys, time, requests
from pathlib import Path
from datetime import datetime
from threading import Lock

from flask import Flask, request, render_template_string, send_from_directory, redirect, url_for, flash, Response

app = Flask(__name__)
app.secret_key = "change-this-to-something-very-random"

BASE_DIR = Path.home() / "deployments"
META_FILE = BASE_DIR / "meta.json"
BASE_DIR.mkdir(exist_ok=True)
if not META_FILE.exists():
    META_FILE.write_text("{}")

# Track running subprocesses {site_id: {"process": Popen, "port": int}}
running = {}
proc_lock = Lock()

# Base port for dynamic apps
PORT_START = 8000

# ---------- helpers ----------
def load_meta():
    with open(META_FILE) as f:
        return json.load(f)

def save_meta(data):
    with open(META_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def get_unique_id():
    return uuid.uuid4().hex[:8]

def extract_zip(zip_path, extract_to):
    """Extract ZIP, flattening a single top-level folder if present."""
    with zipfile.ZipFile(zip_path, 'r') as zf:
        # Check if all files are inside a single top-level folder
        members = zf.namelist()
        prefix = os.path.commonprefix(members).rstrip('/')
        has_single_folder = prefix and all(m.startswith(prefix + '/') for m in members if m != prefix + '/')
        
        for member in zf.infolist():
            if has_single_folder and member.filename.startswith(prefix + '/'):
                # Remove the top folder
                target = member.filename[len(prefix)+1:]
            else:
                target = member.filename
            
            if not target:  # skip the folder itself
                continue
            target_path = extract_to / target
            if member.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
            else:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as source, open(target_path, 'wb') as out:
                    shutil.copyfileobj(source, out)

def find_entry_point(site_dir):
    """Find a Python web app entry file. Looks for common patterns."""
    candidates = ['app.py', 'main.py', 'application.py', 'wsgi.py', 'server.py']
    for c in candidates:
        path = site_dir / c
        if path.exists():
            # Check if it contains 'app = Flask' or similar
            content = path.read_text()
            if 'Flask(' in content or 'FastAPI' in content or 'app =' in content:
                return c
    return None

def install_requirements(site_dir):
    """Install pip requirements into a local lib folder."""
    req_file = site_dir / "requirements.txt"
    if not req_file.exists():
        return True
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--target", str(site_dir / "lib")],
            check=True, capture_output=True, text=True
        )
        return True
    except subprocess.CalledProcessError as e:
        app.logger.error(f"pip install failed: {e.stderr}")
        return False

def get_available_port():
    """Find the next free port starting from PORT_START."""
    used = {info['port'] for info in running.values()}
    port = PORT_START
    while port in used:
        port += 1
    return port

def start_app_process(site_id, site_dir, entry):
    """Launch the Python app as a subprocess on a free port."""
    port = get_available_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(site_dir) + os.pathsep + str(site_dir / "lib")
    env["FLASK_APP"] = entry
    # Use gunicorn if available, else fallback to flask dev server (works for demo)
    try:
        # Prefer gunicorn for production, but not required
        subprocess.run([sys.executable, "-m", "gunicorn", "--version"], capture_output=True)
        cmd = [sys.executable, "-m", "gunicorn", "-w", "1", "-b", f"127.0.0.1:{port}", f"{entry[:-3]}:app"]
    except:
        # Fallback: run the entry file directly (assumes Flask with app.run)
        cmd = [
            sys.executable, "-c",
            f"import sys; sys.path.insert(0, '{site_dir}'); sys.path.insert(0, '{site_dir}/lib'); "
            f"from {entry[:-3]} import app; app.run(host='127.0.0.1', port={port})"
        ]
    
    proc = subprocess.Popen(cmd, env=env, cwd=str(site_dir), preexec_fn=os.setsid)
    # Give it a moment to start
    time.sleep(2)
    return proc, port

def stop_app_process(site_id):
    """Kill the process group for a site."""
    with proc_lock:
        info = running.get(site_id)
        if not info:
            return
        proc = info["process"]
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except:
            proc.kill()
        del running[site_id]

# ---------- routes ----------
@app.route('/')
def index():
    meta = load_meta()
    # Enrich with running status
    for sid in meta:
        meta[sid]["running"] = sid in running
    return render_template_string(HTML_INDEX, sites=meta)

@app.route('/upload', methods=['POST'])
def upload():
    if 'site' not in request.files:
        flash('No file part')
        return redirect(url_for('index'))
    file = request.files['site']
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('index'))
    if not file.filename.lower().endswith('.zip'):
        flash('Only .zip files allowed')
        return redirect(url_for('index'))

    site_id = get_unique_id()
    site_dir = BASE_DIR / site_id
    zip_path = BASE_DIR / f"{site_id}.zip"

    try:
        file.save(zip_path)
        extract_zip(zip_path, site_dir)
    except Exception as e:
        flash(f'Extraction failed: {e}')
        if site_dir.exists():
            shutil.rmtree(site_dir)
        return redirect(url_for('index'))
    finally:
        if zip_path.exists():
            os.remove(zip_path)

    # Detect Python app
    entry = find_entry_point(site_dir)
    if not entry:
        shutil.rmtree(site_dir)
        flash('No Python web app found. ZIP must contain a file like app.py with a Flask/FastAPI "app" object.')
        return redirect(url_for('index'))

    # Install dependencies
    if not install_requirements(site_dir):
        shutil.rmtree(site_dir)
        flash('Failed to install dependencies. Check your requirements.txt.')
        return redirect(url_for('index'))

    # Save metadata
    meta = load_meta()
    meta[site_id] = {
        "id": site_id,
        "name": request.form.get('name', site_id),
        "created": datetime.now().isoformat(),
        "type": "python",
        "entry": entry,
        "status": "stopped"
    }
    save_meta(meta)
    flash(f'App "{meta[site_id]["name"]}" uploaded. Start it to go live.')
    return redirect(url_for('index'))

@app.route('/start/<site_id>')
def start_site(site_id):
    meta = load_meta()
    if site_id not in meta or meta[site_id]["type"] != "python":
        flash("Invalid app")
        return redirect(url_for('index'))

    if site_id in running:
        flash("Already running")
        return redirect(url_for('index'))

    site_dir = BASE_DIR / site_id
    entry = meta[site_id]["entry"]
    try:
        proc, port = start_app_process(site_id, site_dir, entry)
        with proc_lock:
            running[site_id] = {"process": proc, "port": port}
        meta[site_id]["status"] = "running"
        meta[site_id]["port"] = port
        save_meta(meta)
        flash(f'App started on port {port}. Visit /sites/{site_id}')
    except Exception as e:
        flash(f'Failed to start: {e}')
    return redirect(url_for('index'))

@app.route('/stop/<site_id>')
def stop_site(site_id):
    if site_id in running:
        stop_app_process(site_id)
        meta = load_meta()
        meta[site_id]["status"] = "stopped"
        save_meta(meta)
        flash("App stopped")
    else:
        flash("Not running")
    return redirect(url_for('index'))

@app.route('/delete/<site_id>')
def delete_site(site_id):
    if site_id in running:
        stop_app_process(site_id)
    site_dir = BASE_DIR / site_id
    if site_dir.exists():
        shutil.rmtree(site_dir)
    meta = load_meta()
    meta.pop(site_id, None)
    save_meta(meta)
    flash("App deleted")
    return redirect(url_for('index'))

# ---------- Reverse proxy to subprocess apps ----------
@app.route('/sites/<site_id>', defaults={'path': ''})
@app.route('/sites/<site_id>/<path:path>')
def proxy_site(site_id, path):
    meta = load_meta()
    if site_id not in meta or meta[site_id]["type"] != "python":
        return "Site not found", 404

    if site_id not in running:
        return "App is not running. Start it first.", 503

    port = running[site_id]["port"]
    target_url = f"http://127.0.0.1:{port}/{path}"
    # Forward request
    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers={key: value for key, value in request.headers if key != 'Host'},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True
        )
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for name, value in resp.raw.headers.items() if name.lower() not in excluded_headers]
        return Response(resp.content, resp.status_code, headers)
    except requests.ConnectionError:
        return "App is not responding", 502

# ---------- HTML template ----------
HTML_INDEX = """
<!DOCTYPE html>
<html>
<head>
    <title>Python App Deployer</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, sans-serif; padding: 15px; max-width: 800px; margin: auto; background: #f5f5f5; }
        .container { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .flash { background: #e8f5e9; border-left: 4px solid #4caf50; padding: 10px; margin-bottom: 15px; border-radius: 4px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 12px 8px; border-bottom: 1px solid #ddd; text-align: left; }
        th { background: #f8f9fa; }
        .btn { padding: 5px 10px; color: white; border-radius: 5px; text-decoration: none; font-size: 14px; display: inline-block; margin: 2px; }
        .btn-start { background: #4CAF50; }
        .btn-stop { background: #ff9800; }
        .btn-delete { background: #f44336; }
        .btn-visit { background: #2196F3; }
        input, select { padding: 10px; border: 1px solid #ddd; border-radius: 5px; width: 100%; box-sizing: border-box; font-size: 16px; margin-bottom: 10px; }
        input[type="submit"] { background: #4CAF50; color: white; border: none; cursor: pointer; }
        .status { font-weight: bold; }
        .running { color: green; }
        .stopped { color: red; }
    </style>
</head>
<body>
    <div class="container">
        <h2>🐍 Deploy Python Web App</h2>
        {% with messages = get_flashed_messages() %}
          {% if messages %}<div class="flash">{% for m in messages %}{{ m }}<br>{% endfor %}</div>{% endif %}
        {% endwith %}
        <form method="POST" action="/upload" enctype="multipart/form-data">
            <input type="text" name="name" placeholder="App name (optional)">
            <input type="file" name="site" accept=".zip" required>
            <input type="submit" value="Upload & Deploy">
        </form>
        <small>ZIP must contain a Python web app (e.g., Flask) with a file like app.py that exposes an <code>app</code> object.</small>
    </div>

    <div class="container">
        <h3>📦 Your Apps</h3>
        {% if sites %}
        <table>
            <tr><th>Name</th><th>Status</th><th>Actions</th></tr>
            {% for id, info in sites.items() %}
            <tr>
                <td>{{ info.name }}</td>
                <td class="status {% if id in running %}running{% else %}stopped{% endif %}">
                    {{ 'Running' if id in running else 'Stopped' }}
                </td>
                <td>
                    {% if id in running %}
                        <a href="/sites/{{ id }}" class="btn btn-visit" target="_blank">Visit</a>
                        <a href="/stop/{{ id }}" class="btn btn-stop">Stop</a>
                    {% else %}
                        <a href="/start/{{ id }}" class="btn btn-start">Start</a>
                    {% endif %}
                    <a href="/delete/{{ id }}" class="btn btn-delete" onclick="return confirm('Delete forever?')">Delete</a>
                </td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <p>No apps yet. Upload a ZIP with your Python code.</p>
        {% endif %}
    </div>
</body>
</html>
"""

if __name__ == '__main__':
    # Cleanup subprocesses on exit
    def cleanup(sig=None, frame=None):
        for sid in list(running.keys()):
            stop_app_process(sid)
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    # Use threaded=True for development only; better to use gunicorn
    app.run(host='0.0.0.0', port=5000, threaded=True)
