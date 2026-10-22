"""Request bodies of the JSON API (pydantic validates and documents them at /docs)."""

from pydantic import BaseModel

from ..ml import features as feats


class ProjectionRequest(BaseModel):
    feature: str = feats.DEFAULT_FEATURE
    method: str = 'umap'
    params: dict = {}
    classes: list[int] | None = None


class LabelQualityRequest(BaseModel):
    feature: str | None = None
    folds: int | None = None              # None = config analysis.label_quality
    classifier: str | None = None         # logreg | knn | mlp


class GroupsRequest(ProjectionRequest):
    min_cluster_size: int = 15
    per_class: bool = True


class IdsRequest(BaseModel):
    ids: list[int]
    feature: str | None = None


class DistancesRequest(BaseModel):
    ids: list[int]
    feature: str | None = None


class TagRequest(BaseModel):
    tag: str
    ids: list[int]
    remove: bool = False


class TextSearchRequest(BaseModel):
    query: str
    feature: str | None = None
    k: int = 200


# ------------------------------------------------------------ labeling sessions (/api/label/*)
class LabelClassIn(BaseModel):
    name: str
    color: str | None = None
    hotkey: str | None = None


class LabelTaskIn(BaseModel):
    name: str
    task_type: str = 'object_detection'
    config: dict | None = None
    classes: list[LabelClassIn | str] = []


class LabelProjectIn(BaseModel):
    name: str
    description: str | None = None
    source: dict = {'kind': 'upload'}
    tasks: list[LabelTaskIn]
    export_default: dict | None = None
    tags: list[str] | None = None
    review: bool = False


class LabelProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    export_default: dict | None = None
    tags: list[str] | None = None
    review: bool | None = None


class LabelTaskPatch(BaseModel):
    name: str | None = None
    config: dict | None = None


class LabelIdsIn(BaseModel):
    ids: list[str]


class LabelClassesIn(BaseModel):
    names: list[str] | None = None
    classes: list[LabelClassIn] | None = None


class LabelClassPatch(BaseModel):
    name: str | None = None
    color: str | None = None
    hotkey: str | None = None
    position: int | None = None


class LabelImportIn(BaseModel):
    source: dict | None = None


class AnnotationsPut(BaseModel):
    task_id: str
    base_version: int = 0
    annotations: list[dict] = []


class LabelTaskIdsIn(BaseModel):
    task_ids: list[str]


class ReviewIn(BaseModel):
    task_ids: list[str]
    mode: str = 'manual'                 # manual | bulk | clear
    media_ids: list[str] | None = None   # None = every image of the tasks


class ExportIn(BaseModel):
    target: dict = {'kind': 'zip'}
    task_ids: list[str] | None = None
    include_images: bool = True
    only_done: bool = False
    overwrite: bool = False
    classes: dict[str, list[str]] | None = None     # {task_id: [class_id]} to export; a task not listed keeps all


# ------------------------------------------------------------ labeling v2: models, bulk class delete, duplicate
class ModelDetectIn(BaseModel):
    registry_id: str | None = None                  # a model of Settings › Models (models/hub.py)
    connection_id: str | None = None
    connection: dict | None = None                  # an unsaved {url, api_key?, timeout_s?} (the inline form)


class TaskModelIn(BaseModel):
    registry_id: str | None = None                  # a model of Settings › Models, or:
    connection_id: str | None = None                # a model server connection + one of its models
    model_id: str
    version: str | None = None
    conf: float = 0.25
    auto_on_open: bool = False
    class_map: dict[str, str] = {}                  # model class name -> class_id | 'ignore' | 'new'


class PredictIn(BaseModel):
    task_id: str
    conf: float | None = None


class ModelActivateIn(BaseModel):
    on: bool = True


class PredictAllIn(BaseModel):
    task_id: str
    scope: str = 'unlabeled'                        # unlabeled | not_done | all
    conf: float | None = None


class RemoveModelLabelsIn(BaseModel):
    task_id: str
    media_ids: list[str] | None = None


class LabelClassIdsIn(BaseModel):
    class_ids: list[str]


class DuplicateIn(BaseModel):
    name: str | None = None
