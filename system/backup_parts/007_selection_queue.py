"""File FIFO persistante des sélections, indépendante du navigateur et de Flask."""
import fcntl
import uuid
import sys
from contextlib import contextmanager


@contextmanager
def selection_queue_file():
    directory = ensure_layout()['STATUS_DIR']
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, '.selection-queue.json')
    with open(path + '.lock', 'a', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.exists(path):
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
        else:
            data = {'pending': [], 'active': None}
        yield data
        temporary = path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temporary, path)


def selection_queue_snapshot():
    with selection_queue_file() as data:
        return {'pending': list(data['pending']), 'active': data.get('active')}


def selection_queue_cancel(filename):
    with selection_queue_file() as data:
        remaining = [item for item in data['pending'] if item['name'] != filename]
        removed = len(remaining) != len(data['pending'])
        data['pending'] = remaining
    return removed


@backup_bp.route('/api/selection', methods=['POST'])
def enqueue_backup_selection():
    payload = request.get_json(silent=True) or {}
    names = payload.get('filenames') if isinstance(payload, dict) else None
    if not isinstance(names, list) or not names or len(names) > 1000:
        return jsonify(ok=False, message='Veuillez sélectionner au moins un élément.'), 400
    # Valider toute la sélection avant d'enregistrer quoi que ce soit.
    known = {item['name'] for item in list_scripts()}
    if any(not isinstance(name, str) or name not in known for name in names):
        return jsonify(ok=False, message='La sélection contient un script introuvable. Actualisez le tableau.'), 400
    names = list(dict.fromkeys(names))
    added = []
    with selection_queue_file() as data:
        busy = {item['name'] for item in data['pending']}
        if data.get('active'):
            busy.add(data['active']['name'])
        for name in names:
            if name not in busy:
                item = {'id': uuid.uuid4().hex, 'name': name, 'launched': False}
                data['pending'].append(item)
                added.append(item['id'])
        # Une session distincte par demande évite une course avec un worker
        # qui termine. Les workers se sérialisent eux-mêmes avec flock.
        if added:
            settings = ensure_layout()
            token = uuid.uuid4().hex
            ok, message, _, _ = launch_backup_tmux_systemd(
                os.path.join(_NAS_MODULE_DIR, 'backup_selection_worker.py'),
                'selection_' + token + '.py',
                sys.executable,
                os.path.join(settings['LOG_DIR'], 'selection-queue.log'),
            )
            if not ok:
                data['pending'] = [item for item in data['pending'] if item['id'] not in added]
                return jsonify(ok=False, message=message), 500
    return jsonify(ok=True, queue=selection_queue_snapshot())


def run_selection_queue():
    """Un seul worker agit, y compris entre plusieurs processus Gunicorn."""
    directory = ensure_layout()['STATUS_DIR']
    with open(os.path.join(directory, '.selection-worker.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        while True:
            with selection_queue_file() as data:
                if not data.get('active'):
                    if not data['pending']:
                        return
                    data['active'] = data['pending'].pop(0)
                item = dict(data['active'])
            name = item['name']
            try:
                if not item.get('launched'):
                    # Laisser finir les tâches déjà lancées, toutes catégories
                    # confondues, avant de démarrer le prochain élément.
                    while any(s['status'].get('running') for s in list_scripts() if s['name'] != name):
                        time.sleep(2)
                    if not read_status(name).get('running'):
                        from flask import Flask
                        with Flask('backup-selection').test_request_context('/?ajax=1', method='POST'):
                            response, code = run_script(name)
                            result = response.get_json()
                        if code >= 400 or not result.get('ok'):
                            write_status_file(name, running=False, result='Erreur', phase='selection_error',
                                              ended_at=now_label(), message=result.get('message') or 'Lancement impossible')
                    with selection_queue_file() as data:
                        data['active']['launched'] = True
                while read_status(name).get('running'):
                    time.sleep(2)
            except Exception as exc:
                write_status_file(name, running=False, result='Erreur', phase='selection_error',
                                  ended_at=now_label(), message='File de lancement : ' + str(exc))
            with selection_queue_file() as data:
                data['active'] = None
