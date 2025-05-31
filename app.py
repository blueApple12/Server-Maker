import os
import signal
import sys
import psutil                       # used to compute free RAM
from flask import (
    Flask, render_template, redirect, url_for, flash,
    jsonify, request, send_from_directory, abort
)
from flask_wtf import FlaskForm, CSRFProtect
from wtforms import StringField, IntegerField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Regexp, NumberRange
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import logging
import server_runner

# Import SERVER_DIR so the file manager knows where to look
SERVER_DIR = server_runner.SERVER_DIR

# Load environment variables (for SECRET_KEY, etc.)
load_dotenv()

app = Flask(__name__)
# Secret key for session & CSRF protection
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-in-production')
CSRFProtect(app)

# Configure logging
app_logger = logging.getLogger('app_logger')
app_logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
handler.setFormatter(formatter)
app_logger.addHandler(handler)
app.logger = app_logger

# -----------------------
# Flask-WTF Forms
# -----------------------
class CreateServerForm(FlaskForm):
    version = StringField(
        'Minecraft Version',
        validators=[
            DataRequired(message="Version is required."),
            Regexp(r'^\d+\.\d+(?:\.\d+)?$', message="Enter a valid version like '1.21.5'.")
        ]
    )
    ram = IntegerField(
        'RAM (GB)',
        validators=[
            DataRequired(message="RAM is required."),
            NumberRange(min=1, max=64, message="RAM must be between 1 and 64 GB.")
        ]
    )
    submit = SubmitField('Create Server')

class CommandForm(FlaskForm):
    cmd = StringField('Command', validators=[DataRequired(message="Command cannot be empty.")])
    submit = SubmitField('Send')

class UploadForm(FlaskForm):
    path = StringField(
        'Upload To (relative path, leave blank for root)',
        validators=[Regexp(r'^[A-Za-z0-9_\-/.]*$', message="Allowed: letters, numbers, '-', '_', '/', '.'")]
    )
    submit = SubmitField('Upload')

class EditForm(FlaskForm):
    content = TextAreaField('File Content', validators=[DataRequired()])
    submit = SubmitField('Save')


# -----------------------
# Signal handler to gracefully stop the Minecraft subprocess
# -----------------------
def handle_exit(signum, frame):
    app_logger.info(f"Received signal {signum}, stopping Minecraft server...")
    server_runner.stop_server()
    sys.exit(0)

signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


# -----------------------
# Helper: Determine server “state”
# -----------------------
def get_server_state():
    """
    Returns one of "offline", "booting", "online".
    - "offline": server folder doesn't exist or process not running
    - "booting": process is alive but logs do not show any "Done ("
    - "online": process is alive and logs contain "Done ("
    """
    # If the server directory doesn’t exist, it's offline
    if not server_runner.server_exists():
        return "offline"

    # If the process is not running, it's offline
    if not server_runner.is_server_running():
        return "offline"

    # Process is alive. Check logs for any "Done (" substring (case-insensitive)
    logs = server_runner.get_logs().splitlines()
    for line in reversed(logs):
        if "done (" in line.lower():
            return "online"

    # If no "Done (" found yet, it's still booting
    return "booting"


# -----------------------
# ROUTES
# -----------------------

@app.route('/', methods=['GET', 'POST'])
def index():
    # If the server directory doesn't exist, show the create form
    if not server_runner.server_exists():
        form = CreateServerForm()

        # Calculate free RAM in GB (available memory)
        vm = psutil.virtual_memory()
        free_ram = vm.available // (1024 ** 3)  # Convert bytes → GiB

        if form.validate_on_submit():
            version = form.version.data
            requested_ram = form.ram.data

            # Prevent user from requesting more RAM than is available
            if requested_ram > free_ram:
                flash(f"Cannot allocate {requested_ram} GB; only {free_ram} GB is available.", 'danger')
                return render_template('create.html', form=form, free_ram=free_ram)

            success = server_runner.create_server(version, requested_ram)
            if success:
                # Auto-launch the newly created server immediately
                server_runner.launch_server()
                flash(f"Server created (v{version}, {requested_ram} GB) and starting...", 'success')
                return redirect(url_for('index'))
            else:
                flash("Failed to create server. Check logs for details.", 'danger')

        # Render create.html, passing free_ram so the template can display it
        return render_template('create.html', form=form, free_ram=free_ram)

    # If server directory exists, show control panel
    cmd_form = CommandForm()
    return render_template('control.html', cmd_form=cmd_form)


@app.route('/status')
def status():
    """
    Return JSON with:
      - state: "offline"/"booting"/"online"
      - tunnel_up: bool
      - claim_link: str or None
      - joinmc_link: str or None (bare domain)
      - logs: latest buffered log text
      - server_running: bool
    """
    state = get_server_state()
    tunnel_up, claim_link, joinmc_link = server_runner.get_playit_status()
    logs = server_runner.get_logs()
    server_running = server_runner.is_server_running()

    return jsonify({
        "state": state,
        "tunnel_up": tunnel_up,
        "claim_link": claim_link,
        "joinmc_link": joinmc_link,
        "logs": logs,
        "server_running": server_running,
    })


@app.route('/start', methods=['POST'])
def start():
    server_runner.launch_server()
    return jsonify({"started": True})


@app.route('/stop', methods=['POST'])
def stop():
    server_runner.stop_server()
    return jsonify({"stopped": True})


@app.route('/delete', methods=['POST'])
def delete():
    success = server_runner.delete_server()
    return jsonify({"deleted": success})


@app.route('/command', methods=['POST'])
def command():
    form = CommandForm()
    if form.validate_on_submit():
        cmd_text = form.cmd.data.strip()
        if cmd_text:
            sent = server_runner.send_command(cmd_text)
            return jsonify({"sent": sent})
    return jsonify({"sent": False})


# -----------------------
# FILE MANAGER ROUTES
# -----------------------

def secure_server_path(rel_path: str) -> str:
    """
    Given a relative path, ensure it stays inside SERVER_DIR.
    Returns the absolute path under SERVER_DIR, or abort(400) if invalid.
    """
    rel_path = rel_path.replace("\\", "/").lstrip("/")
    abs_path = os.path.realpath(os.path.join(SERVER_DIR, rel_path))
    server_real = os.path.realpath(SERVER_DIR)
    if not (abs_path == server_real or abs_path.startswith(server_real + os.sep)):
        abort(400, description="Invalid path")
    return abs_path

@app.route('/files', methods=['GET', 'POST'])
def files():
    """
    GET: show directory listing under SERVER_DIR (or subfolder via ?path=)
    POST: handle file upload into the specified folder.
    """
    rel_dir = request.args.get('path', '').strip().replace("\\", "/")
    try:
        abs_dir = secure_server_path(rel_dir) if rel_dir else os.path.realpath(SERVER_DIR)
    except:
        flash("Invalid directory path.", 'danger')
        return redirect(url_for('files'))

    if not os.path.exists(abs_dir) or not os.path.isdir(abs_dir):
        flash("Directory invalid.", 'danger')
        return redirect(url_for('files'))

    upload_form = UploadForm()
    if request.method == 'POST' and 'file' in request.files:
        uploaded = request.files['file']
        if uploaded and uploaded.filename:
            filename = secure_filename(uploaded.filename)
            target_rel = request.form.get('path', '').strip().replace("\\", "/")
            try:
                target_dir = secure_server_path(target_rel) if target_rel else os.path.realpath(SERVER_DIR)
            except:
                flash("Invalid upload path.", 'danger')
                return redirect(url_for('files', path=rel_dir))

            os.makedirs(target_dir, exist_ok=True)
            dest_path = os.path.join(target_dir, filename)
            uploaded.save(dest_path)
            flash(f"Uploaded '{filename}' to /{target_rel or ''}", 'success')
            return redirect(url_for('files', path=rel_dir))
        else:
            flash("No file selected.", 'warning')
            return redirect(url_for('files', path=rel_dir))

    # Build directory listing
    entries = []
    for name in sorted(os.listdir(abs_dir)):
        full = os.path.join(abs_dir, name)
        entries.append({
            'name': name,
            'is_dir': os.path.isdir(full),
            'rel_path': os.path.join(rel_dir, name).replace("\\", "/")
        })

    return render_template('files.html',
                           entries=entries,
                           current_rel=rel_dir,
                           upload_form=upload_form)


@app.route('/download')
def download():
    """
    Download a file under SERVER_DIR. Expects ?path=some/relative/path/filename
    (Left here in case you still need raw downloads elsewhere.)
    """
    rel_file = request.args.get('path', '').strip().replace("\\", "/")
    try:
        abs_file = secure_server_path(rel_file)
    except:
        abort(400, description="Invalid file path")

    if not os.path.isfile(abs_file):
        abort(404)

    dir_part = os.path.dirname(abs_file)
    filename = os.path.basename(abs_file)
    return send_from_directory(dir_part, filename, as_attachment=True)


@app.route('/edit_file', methods=['GET', 'POST'])
def edit_file():
    """
    View/Edit a text‐based file. URL: /edit_file?path=relative/path/to/file.txt
    """
    rel_file = request.args.get('path', '').strip().replace("\\", "/")
    if not rel_file:
        flash("No file specified.", 'danger')
        return redirect(url_for('files'))

    try:
        abs_file = secure_server_path(rel_file)
    except:
        abort(400, description="Invalid file path")

    if not os.path.exists(abs_file) or os.path.isdir(abs_file):
        flash("Invalid file.", 'danger')
        return redirect(url_for('files'))

    form = EditForm()
    if form.validate_on_submit():
        try:
            with open(abs_file, 'w', encoding='utf-8') as f:
                f.write(form.content.data)
            flash(f"Saved '{rel_file}'.", 'success')
        except Exception as e:
            flash(f"Error saving file: {e}", 'danger')
        return redirect(url_for('files', path=os.path.dirname(rel_file)))

    try:
        with open(abs_file, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        flash(f"Error reading file: {e}", 'danger')
        return redirect(url_for('files'))

    form.content.data = text
    return render_template('edit_file.html', form=form, rel_file=rel_file)


@app.route('/delete_file', methods=['POST'])
def delete_file():
    """
    Delete a file or empty directory under SERVER_DIR. Expects form field 'path'.
    """
    rel_path = request.form.get('path', '').strip().replace("\\", "/")
    if not rel_path:
        flash("No path specified.", 'danger')
        return redirect(url_for('files'))

    try:
        abs_path = secure_server_path(rel_path)
    except:
        abort(400, description="Invalid path")

    if not os.path.exists(abs_path):
        flash("Path does not exist.", 'danger')
        return redirect(url_for('files'))

    if os.path.isdir(abs_path):
        if os.listdir(abs_path):
            flash("Directory not empty; cannot delete.", 'danger')
            return redirect(url_for('files', path=os.path.dirname(rel_path)))
        else:
            os.rmdir(abs_path)
            flash(f"Directory '/{rel_path}' deleted.", 'success')
            return redirect(url_for('files', path=os.path.dirname(rel_path)))

    try:
        os.remove(abs_path)
        flash(f"File '/{rel_path}' deleted.", 'success')
    except Exception as e:
        flash(f"Error deleting: {e}", 'danger')

    return redirect(url_for('files', path=os.path.dirname(rel_path)))


# -----------------------
# RUN THE APP
# -----------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
