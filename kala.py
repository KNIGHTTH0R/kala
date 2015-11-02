#!/usr/bin/python

import datetime
import os
import json
import uuid

import bottle
from bottle_mongo import MongoPlugin

CORS_HEADERS = {
    'Authorization',
    'Content-Type',
    'Accept',
    'Origin',
    'User-Agent',
    'DNT',
    'Cache-Control',
    'X-Mx-ReqToken',
    'Keep-Alive',
    'X-Request',
    'X-Requested-With',
    'If-Modified-Since'
}


# Code from stackoverflow.com
# Question at http://stackoverflow.com/questions/17262170/bottle-py-enabling-cors-for-jquery-ajax-requests
# Thanks to asker http://stackoverflow.com/users/552894/joern
# Thanks to answerer http://stackoverflow.com/users/593047/ron-rothman
class EnableCors(object):
    name = 'enable_cors'
    api = 2

    def apply(self, fn, context):
        def _enable_cors(*args, **kwargs):
            # set CORS headers
            bottle.response.headers['Access-Control-Allow-Origin'] = '*'
            bottle.response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, OPTIONS'
            bottle.response.headers['Access-Control-Allow-Headers'] = ",".join(CORS_HEADERS)

            if bottle.request.method != 'OPTIONS':
                # actual request; reply with the actual response
                return fn(*args, **kwargs)

        return _enable_cors


app = bottle.Bottle()
app.config.update({
    'mongodb.uri': 'mongodb://localhost:27017/',
    'mongodb.db': 'kala',
    'cors.enable': False,
    'filter.read': True,
    'filter.write': False,
    'filter.staging': 'staging',
    'filter.fields': ['_id'],
    'filter.json': 'filter.JSON'
})

app.config.load_config(os.environ.get('KALA_CONFIGFILE', 'settings.ini'))

if os.environ.get('KALA_FILTER_WRITE'):
    app.config['filter.write'] = True
    app.config['filter.json'] = os.environ.get('KALA_FILTER_JSON', app.config['filter.json'])
    app.config['filter.staging'] = os.environ.get('KALA_FILTER_STAGING', app.config['filter.staging'])

if os.environ.get('KALA_FILTER_READ'):
    app.config['filter.read'] = True
    app.config['filter.fields'] = os.environ.get('KALA_FILTER_FIELDS', app.config['filter.fields'])

if app.config['filter.write'] == 'True':
    with open(app.config['filter.json'], 'r') as data_file:
        app.config['filter.json'] = json.load(data_file)

if app.config['filter.fields'] and isinstance(app.config['filter.fields'], str):
    app.config['filter.fields'] = app.config['filter.fields'].split(',')


app.install(MongoPlugin(
    uri=os.environ.get('KALA_MONGODB_URI', app.config['mongodb.uri']),
    db=os.environ.get('KALA_MONGODB_DB', app.config['mongodb.db']),
    json_mongo=True))

if os.environ.get('KALA_CORS_ENABLE'):
    app.config['cors.enable'] = True


if app.config['cors.enable']:
    app.install(EnableCors())


def _get_json(name):
    result = bottle.request.query.get(name)
    return json.loads(result) if result else None


def _filter_write(mongodb, document):
    if app.config['filter.json'] is None:
        return document
    object_id = mongodb[app.config['filter.staging']].insert(document)
    cursor = mongodb[app.config['filter.staging']].find(filter=app.config['filter.json'])
    # Delete from staging collection after cursor becomes a list, otherwise cursor will produce an empty list.
    documents = list(cursor)
    mongodb[app.config['filter.staging']].remove({'_id': object_id}, 'true')
    return any(doc['_id'] == object_id for doc in documents)


def _respects_whitelist(document):
    """Returns True when the document is acceptable to the whitelist, otherwise False"""
    whitelist = app.config['filter.fields']
    # This is used to filter the JSON object.
    if whitelist is None:
        return True
    # When document is a dictionary, delete any keys which are not in the
    # whitelist, unless they are an operator, in which case we apply the filter to the value.
    if isinstance(document, dict):
        if any(key not in whitelist for (key, value) in document.items()):
            return False
        for key in document.keys():
            if key.startswith('$'):
                if not _respects_whitelist(document[key]):
                    return False
            elif key not in whitelist:
                return False
        return True
    # When document is a list, apply the filter on each item, thus returning a filtered list.
    elif isinstance(document, list):
        return all(_respects_whitelist(item) for item in document)
    # When document is a tuple, return whether first element is in the whitelist.
    # This is used for sort
    elif isinstance(document, tuple):
        return document[0] in whitelist
    # This is used for projection.
    # Note that a JSON object can not contain null values.
    elif document is None:
        return True
    return True


def _filter_aggregate(list_):
    """This is used to filter the aggregate JSON

    Keyword arguments:
    list_ -- The JSON should be a list of dictionaries.
    """
    # The idea is to insert a $project at the start of pipeline that only contains fields in the whitelist.
    # Once filtered, the user can do whatever they want and never touch sensitive data.
    project = {'$project': dict((field, 1) for field in app.config['filter.fields'])}
    list_ = [project] + list_
    return list_


def _filter_read(document):
    """Ensures that the empty or None document does not give results outside the filter"""
    if isinstance(document, dict) and not document.keys or document is None:
        document = dict((field, 1) for field in app.config['filter.fields'])
    return document


def _convert_object_type(document, type_):
    """This is used to convert strings to the correct object type

    :param document:
    document -- The json
    type_ -- The target object type
    """
    if isinstance(document, dict):
        for k, v in document.items():
            document[k] = _convert_object_type(v, type_)
    if isinstance(document, list):
        document = [_convert_object_type(item, type_) for item in document]
    elif isinstance(document, (str, bytes)):
        try:
            if type_ == 'ISODate':
                return datetime.datetime.strptime(document, '%Y-%m-%dT%H:%M:%S.%fZ')
            elif type_ == 'UUID':
                return uuid.UUID(document)
        except ValueError:
            # We pass as we don't need to do anything with the value.
            pass
    return document


def _convert_object(document):
    """This is a wrapper for _convert_object_type()

    :param document:
    document -- This should either be a JSON document or a list of JSON documents.
    """
    document = _convert_object_type(document, 'ISODate')
    document = _convert_object_type(document, 'UUID')
    return document


@app.route('/aggregate/<collection>', method=['GET'])
def get_aggregate(mongodb, collection):
    pipeline = _get_json('pipeline')
    # Should this go in the _filter_aggregate?
    # It's also probably overkill, since $out must be the last item in the pipeline.
    pipeline = list(dictionary for dictionary in pipeline if "$out" not in dictionary)
    if app.config['filter.read']:
        if not _respects_whitelist(pipeline):
            bottle.abort(400, "This kala instance is configured with a read filter. "
                              "You may only reference the fields: "
                              ", ".join(app.config['filter.fields']))
        pipeline = _filter_aggregate(pipeline)
    pipeline = _convert_object(pipeline)
    limit = int(bottle.request.query.get('limit', 100))
    pipeline = pipeline + [{'$limit': limit}]
    cursor = mongodb[collection].aggregate(pipeline=pipeline)
    return {'results': [document for document in cursor]}


@app.route('/<collection>', method=['GET'])
def get(mongodb, collection):
    filter_ = _get_json('filter')
    projection = _get_json('projection')
    skip = int(bottle.request.query.get('skip', 0))
    limit = int(bottle.request.query.get('limit', 100))
    sort = _get_json('sort')

    # Turns a list of lists to a list of tuples.
    # This is necessary because JSON has no concept of "tuple" but pymongo
    # takes a list of tuples for the sort order.
    sort = [tuple(field) for field in sort] if sort else None

    filter_ = _convert_object(filter_) if filter_ else None

    # We use a whitelist read setting to filter what is allowed to be read from the collection.
    # If the whitelist read setting is empty or non existent, then nothing is filtered.
    if app.config['filter.read']:
        if not _respects_whitelist(filter_):
            bottle.abort(400, "The 'filter' parameter references a disallowed field. "
                              "Permitted fields are " + ", ".join(app.config['filter.fields']))
        # Filter must be applied to projection, this is to prevent unrestricted reads.
        # If it is empty, we fill it with only whitelisted values.
        # Else we remove values which are not whitelisted.
        if not _respects_whitelist(projection):
            bottle.abort(400, "The 'projection' parameter references a disallowed field. "
                              "Permitted fields are " + ", ".join(app.config['filter.fields']))
        if not _respects_whitelist(sort):
            bottle.abort(400, "The 'sort' parameter references a disallowed field. "
                              "Permitted fields are " + ", ".join(app.config['filter.fields']))

		# If projection is None or empty, project the whitelist.
        projection = _filter_read(projection)

    cursor = mongodb[collection].find(
        filter=filter_, projection=projection, skip=skip, limit=limit,
        sort=sort
    )

    distinct = bottle.request.query.get('distinct')

    if distinct:
        return {'values': cursor.distinct(distinct)}

    return {'results': [document for document in cursor]}


@app.route('/<collection>', method=['POST', 'OPTIONS'])
def post(mongodb, collection):
    if bottle.request.method == 'OPTIONS' and not app.config['cors.enable']:
        bottle.abort(405, "Method is not supported")

    # We insert the document into a staging collection and then apply a filter JSON.
    # If it returns a result, we can insert that into the actual collection.
    if app.config['filter.write']:
        # Need to convert BSON datatypes
        json_ = _convert_object(bottle.request.json)
        if _filter_write(mongodb, json_):
            object_id = mongodb[collection].insert(json_)
            return {'success': list(mongodb[collection].find({"_id": object_id}))}


def main():
    app.run()


if __name__ == '__main__':
    main()
