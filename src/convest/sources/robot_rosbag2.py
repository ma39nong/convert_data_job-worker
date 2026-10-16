"""Wuji/Sharpa named streams, actual teleop status and lossless sensor arrays."""
import numpy as np
from .gello_rosbag2 import (
    Bag as BaseBag, Series, discover, joint_values, snapshot, timestamp_policy,
    STATUS_MESSAGE_TYPES,
)

FINGERS = ('thumb', 'index', 'middle', 'ring', 'pinky')


def modalities(robot):
    result = {'observation.depths.cam0': {
        'topic': '/cam0/depth/image_raw', 'kind': 'depth', 'dtype': 'uint16',
        'units': 'raw recorded units; no scale inferred'}}
    if robot == 'sharpa':
        for side in ('left', 'right'):
            for finger in FINGERS:
                for kind, dtype in [('deformation', 'uint8'), ('wrench', 'float64')]:
                    result[f'observation.tactile.{side}.{finger}.{kind}'] = {
                        'topic': f'/sharpa/{side}/tactile/{finger}/{kind}',
                        'kind': kind, 'dtype': dtype,
                        'units': ('raw mono8 sensor values' if kind == 'deformation' else
                                  'recorded force xyz, torque xyz; calibration not inferred')}
    return result


class Bag(BaseBag):
    def raw_image(self, reference, kind):
        db, row = reference
        payload = db.execute('SELECT data FROM messages WHERE id=?', (row,)).fetchone()[0]
        msg = self.store.deserialize_cdr(payload, 'sensor_msgs/msg/Image')
        encoding, dtype = ('16uc1', np.dtype('u2')) if kind == 'depth' else ('mono8', np.dtype('u1'))
        if msg.encoding.lower() != encoding:
            raise ValueError(f'Expected {encoding}, received {msg.encoding}')
        width_bytes = msg.width * dtype.itemsize
        if msg.step < width_bytes or len(msg.data) != msg.height * msg.step:
            raise ValueError('Invalid image stride/data size')
        rows = np.asarray(msg.data, dtype=np.uint8).reshape(msg.height, msg.step)
        wire_dtype = dtype.newbyteorder('>' if msg.is_bigendian else '<')
        return np.frombuffer(rows[:, :width_bytes].copy(), dtype=wire_dtype).reshape(
            msg.height, msg.width).astype(dtype, copy=False)


def read_streams(bag, contract):
    topics, names = contract['topics'], contract['source_joint_names']
    robot = 'sharpa' if 'measured_left_hand' in names else 'wuji'
    streams = {}
    def records(key, image=False):
        spec = topics[key]
        if spec['topic'] not in bag.topics:
            raise ValueError(f'Missing required {robot} topic: {spec["topic"]}')
        if any(kind != spec['type'] for _, _, kind in bag.topics[spec['topic']]):
            raise ValueError(f'Unexpected message type: {spec["topic"]}')
        return bag.records(spec['topic'], image=image, include_receive=True)
    for side in ('left', 'right'):
        for part in ('arm', 'hand'):
            key = (f'measured_{side}_{part}' if part == 'arm' or robot == 'sharpa' else f'{side}_hand')
            streams[f'state.{side}_{part}'] = Series.build([
                (t, joint_values(m, names[key], ['position', 'velocity']), received)
                for t, m, received in records(f'{side}_{part}_state')])
        key = f'command_{side}_hand' if robot == 'sharpa' else f'{side}_hand'
        streams[f'action.{side}_hand'] = Series.build([
            (t, joint_values(m, names[key], ['position']), received)
            for t, m, received in records(f'{side}_hand_action')])
    actions = {s: [] for s in ('left', 'right')}
    statuses = {s: [] for s in actions}
    for t, msg, received in records('validated_arm_action'):
        for side in actions:
            value = joint_values(msg, names[f'validated_{side}_arm'], ['position'], allow_absent=True)
            if value is not None:
                actions[side].append((t, value, received))
    for t, msg, received in records('arm_command_status'):
        for side in statuses:
            statuses[side].append((t, (side in msg.accepted_sides, not bool(msg.faults)), received))
    for side in actions:
        streams[f'action.{side}_arm'] = Series.build(actions[side])
        streams[f'status.{side}_arm'] = Series.build(statuses[side])
    if robot == 'wuji':
        hands = {s: [] for s in actions}
        for t, msg, received in records('hand_telemetry_status'):
            if msg.side not in hands:
                raise ValueError(f'Unknown telemetry side: {msg.side}')
            good = msg.state_valid and (not msg.engaged or msg.command_valid)
            hands[msg.side].append((t, (bool(msg.engaged), bool(good)), received))
        for side in hands:
            streams[f'status.{side}_hand'] = Series.build(hands[side])
    for camera in ('cam0', 'cam1', 'cam2'):
        streams[camera] = Series.build(list(records(camera, image=True)))
    for key, spec in modalities(robot).items():
        expected = 'geometry_msgs/msg/WrenchStamped' if spec['kind'] == 'wrench' else 'sensor_msgs/msg/Image'
        if spec['topic'] not in bag.topics or any(t != expected for _, _, t in bag.topics[spec['topic']]):
            raise ValueError(f'Missing or incorrect modality: {spec["topic"]}')
        if spec['kind'] == 'wrench':
            samples = []
            for t, msg, received in bag.records(spec['topic'], include_receive=True):
                w = msg.wrench
                values = np.array([w.force.x, w.force.y, w.force.z, w.torque.x, w.torque.y, w.torque.z], dtype=np.float64)
                if not np.isfinite(values).all():
                    raise ValueError(f'Nonfinite wrench: {spec["topic"]}')
                samples.append((t, values, received))
        else:
            samples = list(bag.records(spec['topic'], image=True, include_receive=True))
        streams[key] = Series.build(samples)
    return streams
