"""Persist already-bound Earth Engine graphs; never select scenes on restore."""
from dataclasses import asdict
import json
from secrets import token_urlsafe
import time

import ee
from .download_models import FrozenSelection, PIPELINE_VERSION, canonical, digest


def provider_scope():
    """Read initialized provider identity without HTTP or credential material."""
    state = ee.data._get_state()
    credentials = state.credentials
    return digest(dict(project=state.cloud_api_user_project,
                       identity=getattr(credentials, 'service_account_email', None) or
                       getattr(credentials, 'account', None) or
                       getattr(credentials, 'quota_project_id', None),
                       credential_type=type(credentials).__name__))


def freeze_selection(scene, export_parameters, *, provider_scope, now=None, ttl=86400):
    graph = scene.ee_object.serialize()
    decoded = json.loads(graph)
    def check(value):
        if isinstance(value, dict):
            name = value.get('functionName', '')
            if name in {'ImageCollection.load', 'Collection.first', 'Collection.toList', 'List.get'}:
                raise ValueError('Selection must be bound to direct native asset IDs')
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(decoded)
    # Round-trip copies prevent mutable request/recipe objects changing a token.
    request = json.loads(canonical(export_parameters))
    recipe = json.loads(canonical(scene.selection_recipe or {'mode': 'single', 'assets': [scene.collection + '/' + scene.scene_id]}))
    metadata = {key: value for key, value in scene.__dict__.items() if key not in {'ee_object', 'selection_recipe'}}
    created = time.time() if now is None else now
    fingerprint = digest(dict(graph=decoded, recipe=recipe, request=request,
                              provider_scope=provider_scope, version=PIPELINE_VERSION))
    return FrozenSelection(token_urlsafe(32), fingerprint, request, graph, recipe, metadata,
                           provider_scope, PIPELINE_VERSION, created, created + ttl)


def restore_selection(snapshot):
    return ee.Image(ee.deserializer.fromJSON(snapshot.image_graph))
