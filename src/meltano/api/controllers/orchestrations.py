import os
import subprocess

import pandas as pd
from flask import (
    Blueprint,
    request,
    url_for,
    jsonify,
    make_response,
    Response,
)

from meltano.cli.extract import run_extract, EXTRACTOR_REGISTRY, LOADER_REGISTRY

orchestrationsBP = Blueprint('orchestrations', __name__, url_prefix='/orchestrations')

from sqlalchemy import TIMESTAMP, DATE, DATETIME

from ..config import TEMP_FOLDER, PROJECT_ROOT_DIR
from ..models.settings import Settings

DATETIME_TYPES_TO_PARSE = (TIMESTAMP, DATE, DATETIME)
TRANSFORM_DIR = 'transform'
PROFILES_DIR = 'profile'


@orchestrationsBP.route('/', methods=['GET'])
def index():
    result = {}
    # eh
    return jsonify(result)


@orchestrationsBP.route('/connection_names', methods=['GET'])
def connection_names():
    settings = Settings.query.first()
    if not settings:
        settings = Settings()
    connections = [c['name'] for c in settings.serializable()['settings']['connections']]
    return jsonify(connections)


@orchestrationsBP.route('/run', methods=['POST'])
def run():
    run_output = ''
    out_nl = '\n'
    incoming = request.get_json()
    extractor_name = incoming['extractor']
    loader_name = incoming['loader']
    connection_name = incoming.get('connection_name')

    run_output += f"Loading and initializing extractor: {extractor_name}"
    extractor_class = EXTRACTOR_REGISTRY.get(extractor_name)
    if not extractor_class:
        run_output += out_nl + f'Extractor {extractor_name} not found please specify one of the: {list(EXTRACTOR_REGISTRY.keys())}'
        return jsonify({'append': run_output})
    extractor = extractor_class()

    run_output += out_nl + f"Loading and initializing loader: {loader_name}"
    loader_class = LOADER_REGISTRY.get(loader_name)
    if not loader_class:
        run_output += out_nl + f'Loader {loader_name} not found please specify one of the following: {list(LOADER_REGISTRY.keys())}'
        return jsonify({'append': run_output})

    run_output += out_nl + "Starting extraction"
    results = set()
    for entity in extractor.entities:
        loader = loader_class(
            extractor=extractor,
            entity_name=entity,
        )
        run_output += out_nl + "Applying the schema"
        loader.schema_apply()

        run_output += out_nl + "Extracting Data" + out_nl
        entity_dfs = extractor.extract(entity)
        for df in entity_dfs:
            run_output += "."
            results.add(loader.load(df=df))
    run_output += out_nl + "Load done!"

    run_output += out_nl + "Starting Transform"

    # use the extractor's name as the model name for the transform operation
    transform_log = run_transform(extractor_name, connection_name)

    if transform_log['status'] != 'ok':
        run_output += out_nl + 'ERROR'
    else:
        run_output += out_nl + f"Status: {transform_log['status']}"
        run_output += out_nl + f"Command: {transform_log['command']}"

    run_output += out_nl + out_nl + f"Status:{transform_log['output']}" + out_nl

    return jsonify({'append': run_output})


@orchestrationsBP.route('/extract/<extractor_name>', methods=['POST'])
def extract(extractor_name: str) -> Response:
    """
    endpoint that performs extraction of the selected datasource to the .csv file(s)
    stored on disk in the /static/tmp/ directory)
    """
    csv_files = run_extract(extractor_name, 'csv')
    csv_files_url = [url_for('static', filename=f'tmp/{file_name}') for file_name in csv_files]
    return jsonify({'extractor_name': extractor_name,
                    'output_file_paths': csv_files_url})


@orchestrationsBP.route('/load/<loader_name>', methods=['POST'])
def load(loader_name: str) -> Response:
    """
    endpoint that performs load of the .csv file(s)(stored on disk in the /static/tmp/ directory)
    of the selected extractor  into the selected loader
    :param loader_name: the name of the loader to use in this request
    """
    incoming = request.get_json()
    try:
        extractor_name = incoming['extractor']
        extractor_class = EXTRACTOR_REGISTRY[extractor_name]
        extractor = extractor_class()
    except KeyError:
        return make_response(
            jsonify(
                {'response': 'extractor not found', 'available_extractors': list(EXTRACTOR_REGISTRY.keys())}
            ), 404
        )
    try:
        loader_class = LOADER_REGISTRY.get(loader_name)
    except KeyError:
        return make_response(
            jsonify(
                {'response': 'Loader not found', 'available_loaders': list(LOADER_REGISTRY.keys())}
            ), 404
        )
    extractor_temp_files = [filename for filename in os.listdir(TEMP_FOLDER) if filename.startswith(extractor_name)]
    for file_path in extractor_temp_files:
        filename = os.path.splitext(file_path)[0]
        entity_name = filename.split('--')[1]
        loader = loader_class(entity_name=entity_name, extractor=extractor)
        loader.schema_apply()
        datetime_cols = [col.name for col in loader.table.columns if isinstance(col.type, DATETIME_TYPES_TO_PARSE)]
        df = pd.read_csv(os.path.join(TEMP_FOLDER, file_path), parse_dates=datetime_cols)
        df.replace({pd.NaT: None}, inplace=True)
        loader.load(df=df)
    return jsonify({'response': 'done', 'inserted_files': extractor_temp_files})


@orchestrationsBP.route('/transform/<model_name>', methods=['POST'])
def transform(model_name):
    """
    {
        'connection_name': 'prod_dw',
    }
    Looks up the credential by the name of the datastore passed to the api

    Sets the environment variables and runs dbt in a subprocess for the given model
    dbt run --profiles-dir profile --target meltano_analysis --models <model_name>
    """
    incoming = request.get_json()
    connection_name = incoming.get('connection_name')

    return jsonify(run_transform(model_name, connection_name))


def run_transform(model_name, connection_name):
    settings = Settings.query.first()
    settings_connections = settings.settings['connections']
    connection = next((item for item in settings_connections if item['name'] == connection_name), None)

    if not connection:
        return {
            'status': 'error',
            'output': f'Connection {connection_name} not found',
            'command': '',
        }

    new_env = os.environ.copy()

    if connection['dialect'] == 'postgresql':
        new_env['PG_ADDRESS'] = connection['host']
        new_env['PG_PORT'] = str(connection['port'])
        new_env['PG_USERNAME'] = connection['username']
        new_env['PG_PASSWORD'] = str(connection['password'])
        new_env['PG_DATABASE'] = connection['database']
        new_env['PG_SCHEMA'] = connection['schema']

    run_command = [
        'dbt', 'run',
        '--profiles-dir', f'{PROFILES_DIR}',
        '--target', 'meltano_analysis'
    ]
    if model_name:
        run_command.extend(['--models', f'{model_name}'])

    work_dir = os.getcwd()
    os.chdir(os.path.join(PROJECT_ROOT_DIR, TRANSFORM_DIR))

    command = " ".join(run_command)
    proc = subprocess.Popen(command, env=new_env, shell=True, stdout=subprocess.PIPE)
    transform_log = proc.stdout.read()

    os.chdir(work_dir)

    return {
        'command': str(command),
        'output': transform_log.decode("utf-8"),
        'status': 'ok',
    }
