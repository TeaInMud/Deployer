import os, zipfile, shutil, uuid, json, subprocess, signal, sys, time, requests
from pathlib import Path
from datetime import datetime
from threading import Lock

from flask import Flask, request, render_template_string, redirect, url_for, flash, Response

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-to-something-very-random')

BASE_DIR = Path.home() / "deployments"
META_FILE = BASE_DIR / "meta.json"
BASE_DIR.mkdir(exist_ok=True)
if not META_FILE.exists():
    META_FILE.write_text("{}")

# Track running subprocesses {site_id: {"process": Popen, "port": int}}
running = {}
proc_lock = Lock()

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
        members = zf.namelist()
        if not members:
            raise ValueError("Empty ZIP file")
        
        # Check if all files are inside a single top-level folder
        prefix = os.path.commonprefix(members).rstrip('/')
        has_single_folder = prefix and all(
            m == prefix + '/' or m.startswith(prefix + '/') 
            for m in members
        )
        
        for member in zf.infolist():
            if has_single_folder and member.filename != prefix + '/' and member.filename.startswith(prefix + '/'):
                target = member.filename[len(prefix)+1:]
            else:
                target = member.filename
            
            if not target:
                continue
            
            target_path = extract_to / target
            if member.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
            else:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as source, open(target_path, 'wb') as out:
                    shutil.copyfileobj(source, out)

def find_entry_point(site_dir):
    """Scan directory for Python file containing a web app."""
    search_paths = [site_dir] + [p for p in site_dir.iterdir() if p.is_dir()]
    
    for directory in search_paths:
        for py_file in directory.glob("*.py"):
            # Skip obvious non-app files
            if py_file.name.startswith("_") or py_file.name in {"setup.py", "wsgi.py", "config.py"}:
                continue
            
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
            except:
                continue
            
            # Check for Flask or FastAPI app
            if (("Flask(" in content or "FastAPI(" in content) and "app =" in content) or \
               ("Flask(" in content and "application =" in content):
                return str(py_file.relative_to(site_dir))
    
    return None

def install_requirements(site_dir):
    """Install pip requirements into a local lib folder."""
    req_file = site_dir / "requirements.txt"
    if not req_file.exists():
        return True
    
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file), 
             "--target", str(site_dir / "lib"), "--upgrade"],
            check=True, capture_output=True, text=True, timeout=120
        )
        return True
    except subprocess.CalledProcessError as e:
        app.logger.error(f"pip install failed: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        app.logger.error("pip install timed out")
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
    
    # Determine how to run the app
    try:
        # Check if gunicorn is available (preferred for production)
        subprocess.run([sys.executable, "-m", "gunicorn", "--version"], 
                      capture_output=True, timeout=5)
        module_name = entry[:-3].replace("/", ".")
        cmd = [sys.executable, "-m", "gunicorn", "-w", "1", 
               "-b", f"127.0.0.1:{port}", f"{module_name}:app"]
    except:
        # Fallback: Run the entry file directly (for Flask/FastAPI dev server)
        entry_path = site_dir / entry
        cmd = [sys.executable, entry_path]
        env["FLASK_APP"] = entry
        env["FLASK_RUN_PORT"] = str(port)
        env["FLASK_RUN_HOST"] = "127.0.0.1"
    
    proc = subprocess.Popen(
        cmd, 
        env=env, 
        cwd=str(site_dir),
        preexec_fn=os.setsid if os.name != 'nt' else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait for the app to start
    time.sleep(3)
    
    # Check if process is still running
    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        error_msg = stderr.decode('utf-8', errors='ignore')[:500]
        raise RuntimeError(f"App failed to start:\n{error_msg}")
    
    return proc, port

def stop_app_process(site_id):
    """Kill the process group for a site."""
    with proc_lock:
        info = running.get(site_id)
        if not info:
            return
        
        proc = info["process"]
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=10)
        except:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except:
                pass
        
        del running[site_id]

def get_running_status():
    """Get status of all running apps."""
    status = {}
    for site_id, info in running.items():
        proc = info["process"]
        status[site_id] = proc.poll() is None  # None means still running
    return status

# ---------- routes ----------
@app.route('/')
def index():
    meta = load_meta()
    running_status = get_running_status()
    
    # Enrich metadata with running status
    for site_id in meta:
        meta[site_id]["running"] = running_status.get(site_id, False)
    
    return render_template_string(HTML_INDEX, sites=meta)

@app.route('/upload', methods=['POST'])
def upload():
    if 'site' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('index'))
    
    file = request.files['site']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('index'))
    
    if not file.filename.lower().endswith('.zip'):
        flash('Only .zip files allowed', 'danger')
        return redirect(url_for('index'))

    site_id = get_unique_id()
    site_dir = BASE_DIR / site_id
    zip_path = BASE_DIR / f"{site_id}.zip"

    try:
        file.save(zip_path)
        extract_zip(zip_path, site_dir)
    except Exception as e:
        flash(f'Extraction failed: {str(e)}', 'danger')
        if site_dir.exists():
            shutil.rmtree(site_dir)
        return redirect(url_for('index'))
    finally:
        if zip_path.exists():
            os.remove(zip_path)

    # Detect entry point
    entry = request.form.get('entry', '').strip()
    
    if entry:
        # Manual entry specified
        entry_path = site_dir / entry
        if not entry_path.exists():
            # Try to find it recursively
            found = list(site_dir.rglob(entry))
            if found:
                entry = str(found[0].relative_to(site_dir))
            else:
                shutil.rmtree(site_dir)
                flash(f'Entry file "{entry}" not found in ZIP.', 'danger')
                return redirect(url_for('index'))
    else:
        # Auto-detect
        entry = find_entry_point(site_dir)
    
    if not entry:
        # No entry found – show available Python files
        python_files = list(site_dir.rglob("*.py"))
        file_list = ", ".join(str(f.relative_to(site_dir)) for f in python_files[:10])
        shutil.rmtree(site_dir)
        flash(f'No Python web app found. Found .py files: {file_list}. Include a valid Flask/FastAPI app or specify the entry file name.', 'danger')
        return redirect(url_for('index'))

    # Verify it's actually a web app
    entry_path = site_dir / entry
    try:
        content = entry_path.read_text(encoding="utf-8", errors="ignore")
        if not ("Flask(" in content or "FastAPI(" in content) or "app =" not in content:
            shutil.rmtree(site_dir)
            flash(f'File "{entry}" doesn\'t appear to be a web app. Make sure it contains "app = Flask(__name__)" or similar.', 'danger')
            return redirect(url_for('index'))
    except:
        shutil.rmtree(site_dir)
        flash(f'Cannot read "{entry}". Make sure it\'s a valid Python file.', 'danger')
        return redirect(url_for('index'))

    # Install dependencies
    if not install_requirements(site_dir):
        shutil.rmtree(site_dir)
        flash('Failed to install dependencies. Check your requirements.txt.', 'danger')
        return redirect(url_for('index'))

    # Save metadata
    meta = load_meta()
    meta[site_id] = {
        "id": site_id,
        "name": request.form.get('name', '').strip() or site_id,
        "created": datetime.now().isoformat(),
        "type": "python",
        "entry": entry,
        "status": "stopped",
        "port": None
    }
    save_meta(meta)
    
    flash(f'App "{meta[site_id]["name"]}" uploaded successfully! Click Start to make it live.', 'success')
    return redirect(url_for('index'))

@app.route('/start/<site_id>')
def start_site(site_id):
    meta = load_meta()
    if site_id not in meta or meta[site_id]["type"] != "python":
        flash("Invalid app", 'danger')
        return redirect(url_for('index'))

    if site_id in running:
        proc = running[site_id]["process"]
        if proc.poll() is None:  # Still running
            flash("App is already running", 'info')
            return redirect(url_for('index'))
        else:
            # Process died, clean up
            del running[site_id]

    site_dir = BASE_DIR / site_id
    entry = meta[site_id]["entry"]
    
    try:
        proc, port = start_app_process(site_id, site_dir, entry)
        with proc_lock:
            running[site_id] = {"process": proc, "port": port}
        
        meta[site_id]["status"] = "running"
        meta[site_id]["port"] = port
        save_meta(meta)
        
        flash(f'App started! Visit /sites/{site_id} to access it.', 'success')
    except Exception as e:
        meta[site_id]["status"] = "error"
        save_meta(meta)
        flash(f'Failed to start: {str(e)}', 'danger')
    
    return redirect(url_for('index'))

@app.route('/stop/<site_id>')
def stop_site(site_id):
    if site_id in running:
        stop_app_process(site_id)
        meta = load_meta()
        meta[site_id]["status"] = "stopped"
        save_meta(meta)
        flash("App stopped", 'info')
    else:
        flash("App is not running", 'info')
    
    return redirect(url_for('index'))

@app.route('/restart/<site_id>')
def restart_site(site_id):
    if site_id in running:
        stop_app_process(site_id)
    
    meta = load_meta()
    if site_id not in meta:
        flash("App not found", 'danger')
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
        
        flash(f'App restarted!', 'success')
    except Exception as e:
        meta[site_id]["status"] = "error"
        save_meta(meta)
        flash(f'Failed to restart: {str(e)}', 'danger')
    
    return redirect(url_for('index'))

@app.route('/delete/<site_id>')
def delete_site(site_id):
    if site_id in running:
        stop_app_process(site_id)
    
    site_dir = BASE_DIR / site_id
    if site_dir.exists():
        shutil.rmtree(site_dir)
    
    meta = load_meta()
    if site_id in meta:
        del meta[site_id]
        save_meta(meta)
    
    flash("App deleted", 'info')
    return redirect(url_for('index'))

# ---------- Reverse proxy to subprocess apps ----------
@app.route('/sites/<site_id>', defaults={'path': ''})
@app.route('/sites/<site_id>/<path:path>')
def proxy_site(site_id, path):
    meta = load_meta()
    if site_id not in meta or meta[site_id]["type"] != "python":
        return "Site not found", 404

    if site_id not in running:
        return """
        <html><body>
        <h1>App Not Running</h1>
        <p>This app is not currently running. <a href="/start/{}">Start it here</a>.</p>
        </body></html>
        """.format(site_id), 503
    
    proc = running[site_id]["process"]
    if proc.poll() is not None:
        # Process died
        del running[site_id]
        meta[site_id]["status"] = "stopped"
        save_meta(meta)
        return "App crashed or stopped unexpectedly", 503

    port = running[site_id]["port"]
    target_url = f"http://127.0.0.1:{port}/{path}"
    
    try:
        # Forward the request
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers={key: value for key, value in request.headers if key.lower() != 'host'},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=30
        )
        
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for name, value in resp.raw.headers.items() 
                   if name.lower() not in excluded_headers]
        
        return Response(resp.content, resp.status_code, headers)
    except requests.ConnectionError:
        return "App is not responding", 502
    except requests.Timeout:
        return "App request timed out", 504

# ---------- HTML template ----------
HTML_INDEX = """
<!DOCTYPE html>
<html>
<head>
    <title>Python App Deployer</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; 
            padding: 15px; 
            max-width: 800px; 
            margin: auto; 
            background: #f0f2f5; 
            color: #333;
        }
        .container { 
            background: white; 
            padding: 20px; 
            border-radius: 12px; 
            margin-bottom: 20px; 
            box-shadow: 0 2px 8px rgba(0,0,0,0.1); 
        }
        .flash { 
            padding: 12px; 
            margin-bottom: 15px; 
            border-radius: 8px; 
            font-size: 14px;
        }
        .flash-success { background: #d4edda; border-left: 4px solid #28a745; color: #155724; }
        .flash-danger { background: #f8d7da; border-left: 4px solid #dc3545; color: #721c24; }
        .flash-info { background: #d1ecf1; border-left: 4px solid #17a2b8; color: #0c5460; }
        table { 
            width: 100%; 
            border-collapse: collapse; 
            margin-top: 10px; 
        }
        th, td { 
            padding: 12px 8px; 
            border-bottom: 1px solid #e0e0e0; 
            text-align: left; 
        }
        th { 
            background: #f8f9fa; 
            font-weight: 600; 
            font-size: 14px;
        }
        .btn { 
            padding: 6px 12px; 
            color: white; 
            border-radius: 6px; 
            text-decoration: none; 
            font-size: 13px; 
            display: inline-block; 
            margin: 2px; 
            border: none;
            cursor: pointer;
        }
        .btn-start { background: #28a745; }
        .btn-stop { background: #ffc107; color: #333; }
        .btn-restart { background: #17a2b8; }
        .btn-delete { background: #dc3545; }
        .btn-visit { background: #007bff; }
        input[type="text"], input[type="file"], input[type="submit"] {
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 8px;
            width: 100%;
            font-size: 16px;
            margin-bottom: 12px;
        }
        input[type="submit"] { 
            background: #007bff; 
            color: white; 
            border: none; 
            cursor: pointer; 
            font-weight: 600;
        }
        input[type="submit"]:hover { background: #0056b3; }
        .status { 
            font-weight: 600; 
            padding: 4px 8px; 
            border-radius: 4px; 
            font-size: 12px;
        }
        .status-running { background: #d4edda; color: #155724; }
        .status-stopped { background: #f8d7da; color: #721c24; }
        .status-error { background: #fff3cd; color: #856404; }
        .hint { 
            color: #666; 
            font-size: 13px; 
            margin-top: -8px; 
            margin-bottom: 12px; 
        }
        .badge {
            background: #e9ecef;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            color: #495057;
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>🐍 Deploy Python Web App</h2>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for category, message in messages %}
              <div class="flash flash-{{ category }}">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}
        
        <form method="POST" action="/upload" enctype="multipart/form-data">
            <input type="text" name="name" placeholder="App name (optional)">
            <input type="text" name="entry" placeholder="Entry file (e.g., app.py - leave blank for auto-detect)">
            <div class="hint">If your app uses a different main file, specify it here (e.g., chat.py, server.py)</div>
            <input type="file" name="site" accept=".zip" required>
            <input type="submit" value="Upload & Deploy">
        </form>
        <div class="hint">
            ZIP must contain a Python web app with a Flask/FastAPI "app" object and a requirements.txt file.
        </div>
    </div>

    <div class="container">
        <h3>📦 Your Apps</h3>
        {% if sites %}
        <table>
            <tr>
                <th>Name</th>
                <th>Entry</th>
                <th>Status</th>
                <th>Actions</th>
            </tr>
            {% for id, info in sites.items() %}
            <tr>
                <td>{{ info.name }}</td>
                <td><span class="badge">{{ info.entry }}</span></td>
                <td>
                    <span class="status status-{{ info.status }}">
                        {% if info.get('running') %}
                            Running :{{ info.port }}
                        {% else %}
                            {{ info.status }}
                        {% endif %}
                    </span>
                </td>
                <td>
                    {% if info.get('running') %}
                        <a href="/sites/{{ id }}" class="btn btn-visit" target="_blank">Visit</a>
                        <a href="/stop/{{ id }}" class="btn btn-stop">Stop</a>
                        <a href="/restart/{{ id }}" class="btn btn-restart">Restart</a>
                    {% else %}
                        <a href="/start/{{ id }}" class="btn btn-start">Start</a>
                    {% endif %}
                    <a href="/delete/{{ id }}" class="btn btn-delete" onclick="return confirm('Delete this app forever?')">Delete</a>
                </td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <p style="color: #666; text-align: center; padding: 20px;">
            No apps deployed yet. Upload a ZIP file above!
        </p>
        {% endif %}
    </div>
</body>
</html>
"""

# ---------- Cleanup on exit ----------
def cleanup(sig=None, frame=None):
    for site_id in list(running.keys()):
        stop_app_process(site_id)
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

if __name__ == '__main__':
    # For development/testing only - use gunicorn in production
    app.run(host='0.0.0.0', port=5000, threaded=True, debug=False)
