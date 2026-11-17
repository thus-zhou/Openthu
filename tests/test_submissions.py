"""5.3 assignment upload and formal submission: synthetic transport only.

No test contacts a campus service, uploads a real file or submits a real
assignment.  Every request goes through a synthetic transport that is refused
by default in production and counts every call.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import jsonschema

from synthetic_store import make_action_store, unwrap, wrap
from learn_assistant.core import LearnAssistant
from learn_assistant.providers.mock import MockProvider
from thu_assistant import consent, source_state
from thu_assistant.bridge import CampusError
from thu_assistant.submissions import (
    BlockedSubmissionTransport, CODES, FileSetStore, MAX_FILES, SubmissionError,
    SubmissionJournal, Submissions, SyntheticSubmissionTransport, VERSION,
    capability_report, contract, register,
)
from thu_assistant.submissions.models import (
    closed, normalize_assignment, observed_submission, submission_record, verify_submission,
)
from thu_assistant.submissions.manifest import plan_files
from thu_assistant.submissions.transport import references, request_specs

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / 'contracts' / 'v0.2' / 'sources' / 'data-models' / 'submission.schema.json'
READINESS = ROOT / 'contracts' / 'v0.2' / 'sources' / 'adapter-readiness.json'
CLASSIFICATION = ROOT / 'contracts' / 'v0.2' / 'error-classification.json'
AUTH = 'Synthetic human instruction for this exact test transaction'
ASSIGNMENT = 'SYNTHETIC-ASSIGNMENT-1'


def world(tmp_path, behavior='ok', assignment=None, requires_draft=True, fail_at=0):
    store = make_action_store(tmp_path / 'actions')
    files = FileSetStore(tmp_path / 'files', protect=wrap, unprotect=unwrap)
    journal = SubmissionJournal(tmp_path / 'plans', protect=wrap, unprotect=unwrap)
    transport = SyntheticSubmissionTransport(assignment=assignment, behavior=behavior,
                                             requires_draft=requires_draft, fail_at=fail_at)
    client = Submissions(transport=transport, store=store, file_store=files,
                         journal=journal, account_provider=lambda: 'synthetic-account')
    return SimpleNamespace(client=client, transport=transport, store=store, files=files,
                           journal=journal, root=tmp_path)


def make_files(directory, specs=(('a.pdf', b'alpha'), ('b.pdf', b'beta'))):
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, payload in specs:
        path = directory / name
        path.write_bytes(payload)
        paths.append(path)
    return paths


def grant(context, plan_id):
    record = context.store.read(plan_id)
    host = consent.HostConsent(context.store)
    host.put(record, host.issue_for(record))


def prepare(context, paths, **kwargs):
    kwargs.setdefault('assignment_id', ASSIGNMENT)
    return context.client.prepare(file_paths=paths, **kwargs)


def codes(function, *args, **kwargs):
    with pytest.raises(SubmissionError) as info:
        function(*args, **kwargs)
    return info.value.code


# -- contract and capability ------------------------------------------------

def test_contract_is_registered_with_a_closed_business_schema():
    register()
    from thu_assistant import actions
    assert (contract()['version']) == VERSION
    entry = actions.REGISTRY[('learn', 'submit_assignment')]
    assert entry['version'] == VERSION
    assert entry['request_sequence'] == 'ORDERED_UPLOADS_THEN_DRAFT_THEN_FINAL_SUBMIT'
    assert entry['prechecks'] == actions.CHECKS
    assert entry['required'] == ('assignment_id', 'source_record_id', 'source_evidence_id',
                                 'file_set_id', 'manifest_sha256', 'attachments',
                                 'replacement_policy', 'assignment_state')
    # The business object is closed: an unknown field is refused, not ignored.
    from thu_assistant.queries import validate
    with pytest.raises(CampusError) as info:
        validate({'assignment_id': 'A', 'unexpected': 1}, entry['input_schema'])
    assert info.value.code == 'INVALID_ARGUMENTS'


def test_capability_report_matches_the_frozen_readiness_row():
    row = next(item for item in json.loads(READINESS.read_text(encoding='utf-8'))['modules']
               if item['module'] == '5.3')
    report = capability_report()
    assert report['start_conclusion'] == row['start_conclusion'] == 'BLOCKED_SOURCE_EVIDENCE'
    assert report['blocked_scope'] == row['blocked_scope']
    assert report['submit_supported'] is False
    assert report['production_transport'] == 'NOT_IMPLEMENTED'
    assert report['max_files'] == MAX_FILES


def test_every_module_code_is_registered_and_mirrored():
    frozen = json.loads(CLASSIFICATION.read_text(encoding='utf-8'))['codes']
    for code, rule in CODES.items():
        assert frozen[code] == rule[0], code
        assert source_state.ERROR_CODES[code] == rule[0], code
        assert source_state.public_error(code)['code'] == code


def test_the_module_registers_no_public_tool():
    sources = list((ROOT / 'src' / 'thu_assistant' / 'submissions').rglob('*.py'))
    text = '\n'.join(path.read_text(encoding='utf-8') for path in sources)
    assert 'mcp_stdio' not in text
    assert 'def submit(' not in text


# -- preparation ------------------------------------------------------------

def test_prepare_freezes_the_file_set_and_uploads_nothing(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths, note='合成说明')
    assert plan['status'] == 'PREPARED'
    assert plan['stage'] == 'PREPARED_LOCAL'
    assert context.transport.counts['sent'] == 0
    record = context.store.read(plan['plan_id'])
    business = record['business']
    assert [item['name'] for item in business['attachments']] == ['a.pdf', 'b.pdf']
    assert all(len(item['sha256']) == 64 for item in business['attachments'])
    assert business['replacement_policy'] == 'NO_OVERWRITE'
    assert business['source_record_id'] == ASSIGNMENT
    assert business['source_evidence_id'] == 'learn.submission'
    assert len(business['manifest_sha256']) == 64
    assert str(tmp_path) not in json.dumps(business)
    assert plan['plan']['note_present'] is True


def test_prepare_never_touches_the_old_prepare_tool(tmp_path):
    report = tmp_path / 'report.pdf'
    report.write_bytes(b'%PDF-1.4 synthetic report')
    assistant = LearnAssistant(MockProvider(), system_data_root=tmp_path / 'system')
    context = world(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError('the legacy preparation path must never reach a transport')

    context.transport.send = boom
    result = assistant.prepare_submission(assignment_id='assignment-geology-1',
                                          file_paths=[report], note='合成')
    assert result['submit_supported'] is False
    assert result['ready_for_manual_confirmation'] is True
    assert context.transport.counts['upload'] == 0
    assert context.transport.counts['submit'] == 0
    assert context.transport.counts['sent'] == 0


def test_prepare_refuses_an_existing_submission_without_replacement(tmp_path):
    context = world(tmp_path, behavior='existing')
    paths = make_files(tmp_path / 'source')
    assert codes(prepare, context, paths) == 'SUBMISSION_EXISTING_SUBMISSION'
    assert context.transport.counts['sent'] == 0


def test_prepare_refuses_an_unknown_submission_state(tmp_path):
    context = world(tmp_path, assignment={'submitted': None, 'submitted_at': ''})
    paths = make_files(tmp_path / 'source')
    assert codes(prepare, context, paths) == 'SUBMISSION_REPLACEMENT_UNVERIFIED'


def test_prepare_refuses_a_closed_deadline(tmp_path):
    context = world(tmp_path, behavior='closed')
    paths = make_files(tmp_path / 'source')
    assert codes(prepare, context, paths) == 'SUBMISSION_ASSIGNMENT_CLOSED'


@pytest.mark.parametrize('case,expected', [
    ('missing', 'SUBMISSION_FILE_NOT_FOUND'),
    ('directory', 'SUBMISSION_FILE_UNSAFE'),
    ('empty', 'SUBMISSION_FILE_UNSAFE'),
    ('wrong_extension', 'SUBMISSION_FILE_UNSAFE'),
    ('too_large', 'SUBMISSION_FILE_UNSAFE'),
    ('duplicate_name', 'SUBMISSION_INVALID_ARGUMENTS'),
])
def test_prepare_refuses_unsafe_files(tmp_path, case, expected):
    context = world(tmp_path)
    directory = tmp_path / 'source'
    paths = make_files(directory)
    if case == 'missing':
        paths = [directory / 'absent.pdf']
    elif case == 'directory':
        paths = [directory]
    elif case == 'empty':
        empty = directory / 'empty.pdf'
        empty.write_bytes(b'')
        paths = [empty]
    elif case == 'wrong_extension':
        wrong = directory / 'notes.exe'
        wrong.write_bytes(b'payload')
        paths = [wrong]
    elif case == 'too_large':
        big = directory / 'big.pdf'
        big.write_bytes(b'x' * (1048576 + 1))
        paths = [big]
    elif case == 'duplicate_name':
        other = tmp_path / 'other'
        other.mkdir()
        paths = [*paths, other / 'a.pdf']
        (other / 'a.pdf').write_bytes(b'other')
    assert codes(prepare, context, paths) == expected


def test_prepare_refuses_a_symlink(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    link = tmp_path / 'source' / 'link.pdf'
    try:
        link.symlink_to(paths[0])
    except (OSError, NotImplementedError):
        pytest.skip('symlinks are not available on this platform')
    assert codes(prepare, context, [link]) == 'SUBMISSION_FILE_UNSAFE'


def test_prepare_bounds_the_plan_to_the_gate_capacity(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source', [(f'f{index}.pdf', b'x' * (index + 1))
                                             for index in range(9)])
    assert codes(prepare, context, paths) == 'SUBMISSION_LIMIT_EXCEEDED'
    paths = paths[:MAX_FILES]
    plan = prepare(context, paths)
    business = context.store.read(plan['plan_id'])['business']
    assert len(request_specs(business, references(plan['plan_id'], business['attachments']))) == 10


def test_prepare_refuses_multiple_files_when_the_assignment_forbids_them(tmp_path):
    context = world(tmp_path, assignment={'allow_multiple_files': False})
    paths = make_files(tmp_path / 'source')
    assert codes(prepare, context, paths) == 'SUBMISSION_FILE_UNSAFE'


def test_prepare_refuses_an_unknown_replacement_policy(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    assert codes(prepare, context, paths,
                 replacement_policy='OVERWRITE') == 'SUBMISSION_INVALID_ARGUMENTS'


# -- execution --------------------------------------------------------------

def test_execute_uploads_in_order_then_saves_a_draft_and_submits(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'SUCCEEDED'
    assert result['stage'] == 'VERIFIED'
    assert [effect['status'] for effect in result['effects']] == ['SENT'] * 4
    assert 'UPLOAD_CREATES_REMOTE_STAGING' in result['side_effects']
    urls = [item['url'] for item in context.transport.sent_specs]
    assert urls[0].split('/')[-2] == 'upload'
    assert urls[1].split('/')[-2] == 'upload'
    assert urls[2].split('/')[-2] == 'draft'
    assert urls[3].split('/')[-2] == 'submit'
    assert context.transport.counts['upload'] == 2
    assert context.transport.counts['draft'] == 1
    assert context.transport.counts['submit'] == 1


def test_the_request_sequence_is_exactly_the_frozen_one(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    context.client.execute(plan['plan_id'], AUTH)
    business = context.store.read(plan['plan_id'])['business']
    specs = request_specs(business, references(plan['plan_id'], business['attachments']))
    assert [item['url'] for item in context.transport.sent_specs] == [spec.url for spec in specs]
    assert [item['body'] for item in context.transport.sent_specs] == [spec.body for spec in specs]
    assert all('/synthetic/' in spec.url for spec in specs)


def test_the_submission_record_matches_the_frozen_contract(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    record = result['submission']
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator(schema).validate(record)
    assert record['state'] == 'SUBMITTED'
    assert record['is_final'] is True
    assert record['result_observed'] is True
    assert record['remote_submission_id']
    assert record['automatic_replay_allowed'] is False
    assert record['synthetic'] is True
    assert result['verification']['identity_verified'] is True
    assert result['verification']['content_verified'] is True
    assert result['verification']['verified'] is True


def test_public_result_has_no_local_paths_or_content(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths, note='很私人的说明')
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    text = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path) not in text
    assert 'alpha' not in text
    assert '很私人的说明' not in text
    assert result['plan']['note_present'] is True


def test_a_changed_file_invalidates_the_plan(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    paths[0].write_bytes(b'alpha-changed')
    report = context.client.adapter.preflight(context.store.read(plan['plan_id']))
    assert report['checks']['fees'] is False
    assert 'SUBMISSION_FILE_CHANGED' in report['check_errors']
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED'
    assert result['reason'] == 'PRECONDITIONS_CHANGED_PREPARE_AGAIN'
    assert result['stage'] == 'FAILED'
    assert context.transport.counts['sent'] == 0


def test_a_deleted_file_invalidates_the_plan(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    paths[1].unlink()
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED'
    assert context.transport.counts['sent'] == 0


def test_second_upload_failure_keeps_the_first_effect(tmp_path):
    context = world(tmp_path, behavior='lost_upload', fail_at=1)
    paths = make_files(tmp_path / 'source', [(f'{name}.pdf', payload) for name, payload in
                                             (('a', b'alpha'), ('b', b'beta'), ('c', b'gamma'))])
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'UNKNOWN'
    effects = {item['step']: item['status'] for item in result['effects']}
    assert effects['upload-0'] == 'SENT'
    assert effects['upload-1'] == 'UNKNOWN'
    assert 'draft' not in effects and 'submit' not in effects
    statuses = [item['status'] for item in result['files']]
    assert statuses == ['ACCEPTED', 'UNKNOWN', 'PENDING']
    assert result['files'][0]['receipt_present'] is True
    assert context.transport.counts['upload'] == 2
    before = dict(context.transport.counts)
    context.client.result(plan['plan_id'])
    context.client.result(plan['plan_id'])
    assert context.transport.counts['upload'] == before['upload']
    assert context.transport.counts['submit'] == 0


def test_a_lost_upload_response_is_never_replayed(tmp_path):
    context = world(tmp_path, behavior='lost_upload', fail_at=0)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'UNKNOWN'
    assert result['stage'] == 'UPLOADED_STAGING'
    assert context.transport.counts['upload'] == 1
    for _ in range(3):
        context.client.result(plan['plan_id'])
    assert context.transport.counts['upload'] == 1
    assert context.transport.counts['draft'] == 0
    assert context.transport.counts['submit'] == 0


def test_a_saved_draft_is_not_a_submission(tmp_path):
    context = world(tmp_path, behavior='draft_only')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'UNKNOWN'
    assert result['stage'] == 'DRAFT_SAVED'
    assert result['submission']['state'] == 'DRAFT_SAVED'
    assert result['submission']['is_final'] is False
    assert result['verification']['verified'] is False
    assert result['last_error'] == 'SUBMISSION_PLATFORM_REJECTED'
    assert context.transport.counts['submit'] == 1
    context.client.result(plan['plan_id'])
    assert context.transport.counts['submit'] == 1


def test_a_lost_submit_response_is_resolved_by_readback(tmp_path):
    context = world(tmp_path, behavior='submit_lost')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'SUCCEEDED'
    assert result['stage'] == 'VERIFIED'
    assert context.transport.counts['submit'] == 1
    assert context.transport.counts['sent'] == 4


def test_a_submission_without_an_observed_result_stays_unknown(tmp_path):
    context = world(tmp_path, behavior='submit_unknown')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'UNKNOWN'
    assert result['submission']['state'] == 'UNKNOWN'
    assert 'RESULT_NOT_OBSERVED' in result['submission']['warnings']
    assert result['verification']['verified'] is False


def test_names_only_observations_never_claim_hash_equality(tmp_path):
    context = world(tmp_path, behavior='names_only')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'SUCCEEDED'
    assert result['verification']['identity_verified'] is True
    assert result['verification']['content_verified'] is False
    assert 'CONTENT_HASH_NOT_AVAILABLE' in result['verification']['limitations']


def test_an_observation_for_another_assignment_is_not_matched(tmp_path):
    context = world(tmp_path, behavior='foreign')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'UNKNOWN'
    assert result['submission'] is None


def test_a_superseded_submission_is_not_a_success(tmp_path):
    context = world(tmp_path, behavior='superseded')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED'


def test_a_remote_failure_is_a_failure(tmp_path):
    context = world(tmp_path, behavior='failed')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED'


def test_official_confirmation_hands_off_without_a_write(tmp_path):
    context = world(tmp_path, behavior='official_confirmation')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'AWAITING_OFFICIAL'
    assert context.transport.counts['sent'] == 0
    assert 'COMPLETE_THE_STEP_ON_THE_OFFICIAL_SITE' in result['next_actions']


def test_replacement_with_a_matching_version_is_submitted(tmp_path):
    context = world(tmp_path, behavior='existing')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths, replacement_policy='REPLACE_EXISTING', expected_version=3)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'SUCCEEDED'
    submit = json.loads(context.transport.sent_specs[-1]['body'])
    assert submit['expected_version'] == 3
    assert result['verification']['version_verified'] is True


def test_a_changed_existing_version_blocks_a_replacement(tmp_path):
    context = world(tmp_path, behavior='existing_changes')
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths, replacement_policy='REPLACE_EXISTING', expected_version=3)
    context.transport.change_existing_version()
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED'
    assert context.transport.counts['sent'] == 0


def test_replacement_without_a_version_is_refused(tmp_path):
    context = world(tmp_path, behavior='existing')
    paths = make_files(tmp_path / 'source')
    assert codes(prepare, context, paths, replacement_policy='REPLACE_EXISTING') == \
        'SUBMISSION_REPLACEMENT_UNVERIFIED'


def test_executing_without_a_human_grant_sends_nothing(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    with pytest.raises(CampusError) as info:
        context.client.execute(plan['plan_id'], AUTH)
    assert info.value.code == 'TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED'
    assert context.transport.counts['sent'] == 0
    assert context.store.read(plan['plan_id'])['state'] == 'PREPARED'


def test_the_write_switch_blocks_execution_without_losing_the_plan(tmp_path, monkeypatch):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    monkeypatch.setenv('THU_LEARN_SUBMIT', '0')
    with pytest.raises(SubmissionError) as info:
        context.client.execute(plan['plan_id'], AUTH)
    assert info.value.code == 'SUBMISSION_WRITES_DISABLED'
    assert context.transport.counts['sent'] == 0
    assert context.store.read(plan['plan_id'])['state'] == 'PREPARED'


def test_concurrent_execution_uploads_once(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])

    def run(_):
        try:
            return context.client.execute(plan['plan_id'], AUTH)['status']
        except CampusError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, range(2)))
    assert 'SUCCEEDED' in outcomes
    assert context.transport.counts['upload'] == 2
    assert context.transport.counts['submit'] == 1


def test_a_transport_without_a_draft_step_still_completes(tmp_path):
    context = world(tmp_path, requires_draft=False)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'SUCCEEDED'
    assert context.transport.counts['draft'] == 0
    assert context.transport.counts['sent'] == 3


def test_a_file_set_from_another_account_is_refused(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    record = context.store.read(plan['plan_id'])
    record['account'] = 'another-account'
    with pytest.raises(SubmissionError) as info:
        context.client.adapter.preflight(record)
    assert info.value.code == 'SUBMISSION_PLAN_MISMATCH'


def test_a_platform_rejection_is_recorded_and_not_replayed(tmp_path):
    context = world(tmp_path, behavior='reject_upload', fail_at=0)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    result = context.client.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'UNKNOWN'
    assert result['last_error'] == 'SUBMISSION_PLATFORM_REJECTED'
    assert context.transport.counts['upload'] == 1
    context.client.result(plan['plan_id'])
    assert context.transport.counts['upload'] == 1
    assert context.transport.counts['submit'] == 0


# -- production boundary ----------------------------------------------------

def test_the_default_client_refuses_a_production_transport(tmp_path):
    client = Submissions(
        store=make_action_store(tmp_path / 'actions'),
        file_store=FileSetStore(tmp_path / 'files', protect=wrap, unprotect=unwrap),
        journal=SubmissionJournal(tmp_path / 'plans', protect=wrap, unprotect=unwrap),
        account_provider=lambda: 'synthetic-account')
    assert isinstance(client.transport, BlockedSubmissionTransport)
    paths = make_files(tmp_path / 'source')
    assert codes(client.prepare, assignment_id=ASSIGNMENT,
                 file_paths=paths) == 'SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED'


def test_a_transport_that_does_not_declare_itself_synthetic_is_refused(tmp_path):
    class RealTransport(SyntheticSubmissionTransport):
        synthetic = False

    store = make_action_store(tmp_path / 'actions')
    client = Submissions(
        transport=RealTransport(), store=store,
        file_store=FileSetStore(tmp_path / 'files', protect=wrap, unprotect=unwrap),
        journal=SubmissionJournal(tmp_path / 'plans', protect=wrap, unprotect=unwrap),
        account_provider=lambda: 'synthetic-account')
    paths = make_files(tmp_path / 'source')
    assert codes(client.prepare, assignment_id=ASSIGNMENT,
                 file_paths=paths) == 'SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED'
    assert client.transport.counts['sent'] == 0


# -- storage, rollback and read-only behaviour ------------------------------

def test_the_file_set_and_journal_are_owner_only(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    if os.name == 'nt':
        pytest.skip('POSIX permission bits are not applicable on Windows')
    for directory in ('files', 'plans'):
        for path in (tmp_path / directory).glob('*.dpapi'):
            assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert list((tmp_path / 'plans').glob(plan['plan_id'] + '.dpapi'))


def test_a_corrupt_journal_is_classified(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    (tmp_path / 'plans' / (plan['plan_id'] + '.dpapi')).write_bytes(b'not a submission record')
    with pytest.raises(SubmissionError) as info:
        context.client.result(plan['plan_id'])
    assert info.value.code == 'SUBMISSION_LOCAL_STATE_CORRUPT'


def test_the_synthetic_codec_is_not_a_production_fallback(tmp_path):
    synthetic = FileSetStore(tmp_path / 'files', protect=wrap, unprotect=unwrap)
    assignment, _ = normalize_assignment(
        {'allowed_extensions': ['pdf'], 'max_bytes': 1048576, 'allow_multiple_files': True},
        ASSIGNMENT)
    files, _, _ = plan_files(make_files(tmp_path / 'source'), assignment)
    file_set_id = synthetic.put(operation_digest='0' * 64, account='synthetic-account',
                                assignment_id=ASSIGNMENT, files=files)
    production = FileSetStore(tmp_path / 'files')
    with pytest.raises(SubmissionError) as info:
        production.get(file_set_id)
    assert info.value.code in ('SUBMISSION_LOCAL_STATE_CORRUPT',
                               'SUBMISSION_LOCAL_STATE_UNAVAILABLE')


def test_a_local_cancel_never_claims_a_remote_change(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    context.client.execute(plan['plan_id'], AUTH)
    before = dict(context.transport.counts)
    outcome = context.client.cancel(plan['plan_id'])
    assert outcome['cancelled'] is False
    assert outcome['remote_changes'] == 'NONE'
    assert dict(context.transport.counts) == before
    assert not hasattr(context.client, 'withdraw')


def test_result_after_success_does_not_resend(tmp_path):
    context = world(tmp_path)
    paths = make_files(tmp_path / 'source')
    plan = prepare(context, paths)
    grant(context, plan['plan_id'])
    context.client.execute(plan['plan_id'], AUTH)
    before = dict(context.transport.counts)
    shown = context.client.result(plan['plan_id'])
    assert shown['status'] == 'SUCCEEDED'
    assert dict(context.transport.counts) == before


def test_result_of_another_module_plan_is_refused(tmp_path):
    context = world(tmp_path)
    other = context.store.create({
        'version': 1, 'operation_id': '1' * 32, 'service_id': 'sports', 'operation': 'create',
        'account': 'synthetic-account', 'business': {}, 'digest': '0' * 64, 'context': None,
        'adapter_version': 'sports-action-v1', 'expires_at': 0, 'state': 'PREPARED',
        'missing_fields': [], 'reason': None, 'revision': 0, 'events': [],
        'challenge': 'a' * 64, 'consent_protocol': 'host-authorization-v1', 'effects': []})
    with pytest.raises(SubmissionError) as info:
        context.client.result(other['operation_id'])
    assert info.value.code == 'SUBMISSION_PLAN_MISMATCH'


# -- model-level guarantees -------------------------------------------------

def test_a_final_submission_cannot_be_built_without_evidence():
    with pytest.raises(SubmissionError) as info:
        submission_record(assignment_id=ASSIGNMENT, state='SUBMITTED', is_final=True,
                          version=1, remote_submission_id=None, upload_receipt_id=None,
                          submitted_at=None, result_observed=True)
    assert info.value.code == 'SUBMISSION_RESULT_UNVERIFIED'


def test_an_observation_that_claims_a_submission_without_a_receipt_is_downgraded():
    record, warnings = observed_submission(
        {'assignment_id': ASSIGNMENT, 'state': 'SUBMITTED', 'is_final': True, 'version': 2,
         'remote_submission_id': None, 'submitted_at': None, 'result_observed': True,
         'files': [{'name': 'a.pdf', 'size': 5, 'sha256': '0' * 64}]}, ASSIGNMENT)
    assert record['state'] == 'UNKNOWN'
    assert 'REMOTE_SUBMISSION_ID_MISSING' in warnings


def test_a_date_only_deadline_is_reported_with_its_precision():
    assignment, warnings = normalize_assignment(
        {'assignment_id': ASSIGNMENT, 'deadline': '2099-01-02'}, ASSIGNMENT)
    assert assignment['deadline_precision'] == 'DATE'
    assert 'DEADLINE_DATE_PRECISION' in warnings
    assert closed(assignment) is False


def test_an_unreadable_deadline_blocks_instead_of_assuming_it_is_open():
    assignment, _ = normalize_assignment(
        {'assignment_id': ASSIGNMENT, 'deadline': 'not-a-date'}, ASSIGNMENT)
    assert assignment['deadline_precision'] == 'UNVERIFIED'
    assert closed(assignment) is True


def test_a_missing_deadline_is_not_a_closed_assignment():
    assignment, _ = normalize_assignment({'assignment_id': ASSIGNMENT}, ASSIGNMENT)
    assert assignment['deadline_precision'] == 'NONE'
    assert closed(assignment) is False


def test_verification_requires_a_receipt_and_identity():
    record = submission_record(assignment_id=ASSIGNMENT, state='SUBMITTED', is_final=True,
                               version=2, remote_submission_id='SYN-1', upload_receipt_id='U',
                               submitted_at='2026-11-20T22:00:00+08:00', result_observed=True)
    attachments = [{'name': 'a.pdf', 'size': 5, 'sha256': 'a' * 64}]
    assert verify_submission(record, [{'name': 'a.pdf', 'size': 5}], ASSIGNMENT,
                             attachments, 0)['verified'] is True
    assert verify_submission(record, [], ASSIGNMENT, attachments, 0)['verified'] is False
