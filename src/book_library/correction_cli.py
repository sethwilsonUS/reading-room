"""`library correction`: the interface's metadata review workflow from the terminal.

Each action calls the same Library review methods as the review page, so
drafts, decisions, version checks, and saved curation are shared: a draft
started here appears in the interface and vice versa. Drafting and deciding
never change library information; only `apply` saves selected corrections.

Requesting paid AI suggestions is deliberately not offered here (it stays an
explicit interface action under the library's spending controls). Suggestions
already generated appear as review items and recommendations.

Version checks: `show` reports `revision` and `version`. Passing them back
with `--revision`/`--version` makes a write fail if someone changed the review
in between; when omitted, the current values are used.
"""
from __future__ import annotations

import copy
import uuid

ACTIONS = ('show', 'set', 'unset', 'evidence', 'decide', 'recommendation', 'preview', 'apply', 'discard', 'rebase')
ITEM_DECISIONS = ('accept', 'reject', 'keep', 'later', 'undo', 'restore')
RECOMMENDATION_DECISIONS = ('accept', 'keep', 'later', 'undo', 'restore')
FIELD_KEYS = ('key', 'label', 'group', 'kind', 'value', 'status', 'origin', 'evidence', 'editable', 'inherited',
              'allow_inherit', 'options', 'related_fields', 'help', 'example')
ITEM_KEYS = ('id', 'kind', 'field', 'value', 'status', 'reasons', 'evidence', 'stale')
RECOMMENDATION_KEYS = ('id', 'kind', 'fields', 'item_ids', 'question', 'recommendation', 'reason', 'strength',
                       'strength_reason', 'effect', 'status', 'stale')
EVIDENCE_TEXT_CHARS = 500


def add_parser(subparsers):
    command = subparsers.add_parser('correction', help='Review and correct metadata like the interface review page (no paid AI)')
    command.add_argument('action', choices=ACTIONS)
    command.add_argument('target_id', help='Book, section, or passage ID being reviewed')
    command.add_argument('--field', action='append', default=[], help='Review field key from `show`; repeatable for apply')
    command.add_argument('--value', default='', help='set: new value; names one per line, tags comma-separated')
    command.add_argument('--mode', default='', choices=['', 'unknown', 'inherit', 'absent'], help='set, name fields only: record unknown, inherited, or no named person')
    command.add_argument('--short-names', default='', help='set, name fields only: short forms, one per line')
    command.add_argument('--evidence', default=None, help='Where the source shows this value (up to 4,000 characters)')
    command.add_argument('--verified', choices=['yes', 'no'], help='Whether a person checked the source themselves')
    command.add_argument('--item', help='decide/recommendation/preview: item or recommendation ID from `show`')
    command.add_argument('--decision', help='decide: ' + '/'.join(ITEM_DECISIONS) + '; recommendation: ' + '/'.join(RECOMMENDATION_DECISIONS))
    command.add_argument('--select', action='append', default=None, help='recommendation/preview: item IDs to include; repeatable')
    command.add_argument('--replace', action='store_true', help='Allow replacing an existing different draft value')
    command.add_argument('--all', action='store_true', help='apply: save every pending draft change')
    command.add_argument('--revision', help='Revision from `show`')
    command.add_argument('--version', type=int, help='Draft version from `show`')
    command.add_argument('--request-id', help='apply: idempotency key; repeating it never saves twice')


def show(lib, target_id):
    """A bounded, agent-readable view of the review page."""
    from .review_items import review_items
    context = lib.review_context(target_id)
    draft = lib.review_draft(target_id)
    return {'target': context['target'], 'revision': context['revision'], 'version': draft['version'],
            'stale': draft['stale'], 'source_current': context['source_current'],
            'fields': [{key: field[key] for key in FIELD_KEYS if key in field} for field in context['fields']],
            'draft_changes': draft['changes'],
            'items': [{key: item.get(key) for key in ITEM_KEYS} for item in review_items(context, draft)],
            'recommendations': [{key: rec.get(key) for key in RECOMMENDATION_KEYS} for rec in lib.review_recommendations(target_id)],
            'evidence': [{'id': source.get('id'), 'in_target': source.get('in_target'), 'read_url': source.get('read_url'),
                          'text': (source.get('text') or '')[:EVIDENCE_TEXT_CHARS]} for source in context.get('evidence', [])]}


def _require(value, message):
    if not value:
        raise ValueError(message)
    return value


def _one_field(args):
    if len(args.field) != 1:
        raise ValueError('Name exactly one --field from `library correction show`.')
    return args.field[0]


def _choice(value, allowed, action):
    if value not in allowed:
        raise ValueError(f'{action} needs --decision ' + ', '.join(allowed) + '.')
    return value


def _save(lib, target_id, changes, revision, version):
    lib.save_review_draft(target_id, changes, revision=revision, expected_version=version)


def run(lib, args):
    from .review import draft_change
    target = args.target_id
    if args.action == 'show':
        return show(lib, target)
    if args.action == 'preview':
        item = _require(args.item, 'Name the recommendation with --item.')
        return lib.review_recommendation_preview(target, item, args.select)
    context = lib.review_context(target)
    draft = lib.review_draft(target)
    revision = args.revision or context['revision']
    version = draft['version'] if args.version is None else args.version
    changes = copy.deepcopy(draft['changes'])
    result = None
    match args.action:
        case 'set':
            key = _one_field(args)
            fields = {field['key']: field for field in context['fields']}
            if key not in fields or not fields[key].get('editable', True):
                raise ValueError('Choose an editable field listed by `library correction show`.')
            changes[key] = draft_change(fields[key], args.value, changes.get(key), mode=args.mode, short_names=args.short_names,
                                        evidence=args.evidence or '', verified=args.verified == 'yes')
            _save(lib, target, changes, revision, version)
        case 'unset':
            key = _one_field(args)
            if changes.pop(key, None) is None:
                raise ValueError('That field has no pending draft change.')
            _save(lib, target, changes, revision, version)
        case 'evidence':
            key = _one_field(args)
            if key not in changes:
                raise ValueError('That field has no pending draft change; use `set` first.')
            if args.evidence is None and args.verified is None:
                raise ValueError('Give --evidence, --verified, or both.')
            if args.evidence is not None:
                changes[key]['evidence'] = args.evidence.strip()
            if args.verified is not None:
                changes[key]['verified'] = args.verified == 'yes'
            _save(lib, target, changes, revision, version)
        case 'decide':
            item = _require(args.item, 'Name the review item with --item.')
            lib.decide_review(target, item, _choice(args.decision, ITEM_DECISIONS, 'decide'), revision=revision,
                              expected_version=version, replace=args.replace)
        case 'recommendation':
            item = _require(args.item, 'Name the recommendation with --item.')
            lib.decide_recommendation(target, item, _choice(args.decision, RECOMMENDATION_DECISIONS, 'recommendation'),
                                      revision=revision, expected_version=version, selected_item_ids=args.select,
                                      replace=args.replace)
        case 'apply':
            if args.all == bool(args.field):
                raise ValueError('Choose the draft changes to save with --field (repeatable) or --all.')
            selected = list(changes) if args.all else args.field
            missing = [key for key in selected if key not in changes]
            if missing or not selected:
                raise ValueError('No pending draft change for: ' + (', '.join(missing) or 'any field') + '.')
            result = lib.apply_review(target, selected, revision=revision, expected_version=version,
                                      request_id=args.request_id or 'cli-' + uuid.uuid4().hex, changes=changes)
        case 'discard':
            lib.discard_review_draft(target, expected_version=version)
        case 'rebase':
            lib.rebase_review_draft(target, expected_version=version)
    # Mutations return the refreshed review (with its new version); apply also
    # returns its save receipt.
    return {**({'receipt': result} if result is not None else {}), 'review': show(lib, target)}
