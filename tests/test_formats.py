import yaml

from dataviz.labeling import formats

C = {'a': 0, 'b': 1}


def box(i, cls, x, y, w, h):
    return {'id': i, 'class_id': cls, 'geometry_type': 'bbox', 'geometry': {'x': x, 'y': y, 'w': w, 'h': h}}


def test_detect_exact_round_trip():
    rows = [box('1', 'a', 0.1, 0.2, 0.3, 0.4), box('2', 'b', 0.0, 0.0, 1.0, 1.0)]
    txt = formats.yolo_detect(rows, C)
    assert txt == '0 0.250000 0.400000 0.300000 0.400000\n1 0.500000 0.500000 1.000000 1.000000\n'
    assert formats.parse_detect(txt) == [(0, 0.25, 0.4, 0.3, 0.4), (1, 0.5, 0.5, 1.0, 1.0)]
    assert formats.yolo_detect([], C) == ''
    assert formats.yolo_detect([box('3', 'zz', .1, .1, .1, .1)], C) == ''    # unknown / deleted class


def test_seg_round_trip_and_holes_warning():
    pts = [[0.1, 0.1], [0.5, 0.123456789], [0.3, 0.9]]
    w = {}
    rows = [{'id': '1', 'class_id': 'b', 'geometry_type': 'polygon', 'geometry': {'points': pts, 'holes': [pts]}},
            {'id': '2', 'class_id': 'a', 'geometry_type': 'mask_rle', 'geometry': {'size': [2, 2], 'counts': 'x'}}]
    txt = formats.yolo_seg(rows, C, w)
    assert txt == '1 0.100000 0.100000 0.500000 0.123457 0.300000 0.900000\n'
    assert formats.parse_seg(txt) == [(1, [(0.1, 0.1), (0.5, 0.123457), (0.3, 0.9)])]
    assert w == {'holes_dropped': 1, 'mask_rle_dropped': 1}


def test_pose_parent_box_and_extent():
    kp = {'id': 'k', 'class_id': 'a', 'geometry_type': 'keypoints', 'parent_id': 'p',
          'geometry': {'points': [[0.2, 0.3, 2], [0.4, 0.5, 1], [0.9, 0.9, 0]]}}
    parent = box('p', 'a', 0.1, 0.2, 0.4, 0.4)
    txt = formats.yolo_pose([parent, kp], C, 3)
    assert txt == ('0 0.300000 0.400000 0.400000 0.400000 0.200000 0.300000 2 0.400000 0.500000 1 '
                   '0.000000 0.000000 0\n')
    assert formats.parse_pose(txt, 3) == [(0, (0.3, 0.4, 0.4, 0.4), [(0.2, 0.3, 2), (0.4, 0.5, 1), (0.0, 0.0, 0)])]
    kp2 = dict(kp, parent_id=None)
    (_, (cx, cy, w, h), _), = formats.parse_pose(formats.yolo_pose([kp2], C, 3), 3)
    assert (cx, cy, w, h) == (0.3, 0.4, 0.24, 0.24)          # extent of visible points + 2 % padding


def test_cls_multi_label_sorted_unique():
    rows = [{'id': str(i), 'class_id': c, 'geometry_type': 'tag', 'geometry': {}} for i, c in enumerate('bab')]
    assert formats.yolo_cls(rows, C) == '0\n1\n'


def test_class_map_skips_deleted_and_orders_by_position():
    classes = [{'id': 'x', 'name': 'x', 'position': 2}, {'id': 'd', 'name': 'd', 'position': 0, 'is_deleted': 1},
               {'id': 'y', 'name': 'y', 'position': 1}]
    idx, names = formats.class_map(classes)
    assert idx == {'y': 0, 'x': 1} and names == ['y', 'x']
    assert formats.classes_txt(names) == 'y\nx\n'


def test_data_yaml_detect_and_pose():
    d = yaml.safe_load(formats.data_yaml({'name': 'Det', 'task_type': 'object_detection'}, ['cat', 'chó']))
    assert d['names'] == {0: 'cat', 1: 'chó'} and d['nc'] == 2 and d['train'] == '../images'
    p = yaml.safe_load(formats.data_yaml({'name': 'P', 'task_type': 'keypoints',
                                          'config': {'keypoints': ['l', 'r', 'n'], 'flip_idx': [1, 0, 2]}}, ['person']))
    assert p['kpt_shape'] == [3, 3] and p['flip_idx'] == [1, 0, 2]


def test_slugify_unique():
    assert formats.slugify('Object detection!') == 'object-detection'
    assert formats.slugify('Object detection', {'object-detection'}) == 'object-detection-2'
    assert formats.slugify('ดาว') == 'task'
