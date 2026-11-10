"""Accessible review pages: drafts and suggestions never imply accepted curation."""
from __future__ import annotations

from copy import deepcopy
import json
import hashlib
import secrets
from urllib.parse import quote, urlencode, urlsplit

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .citations import _display as contributor_name


def display_value(value):
    if value is None or value == '':
        return 'Not recorded'
    if isinstance(value, list):
        if not value:
            return 'None explicitly recorded'
        return '; '.join(contributor_name(item) if isinstance(item, dict) else str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def field_display(field, value):
    if field.get('kind') == 'select':
        return next((option['label'] for option in field.get('options', []) if option['value'] == value), display_value(value))
    return display_value(value)


def safe_link(value):
    """Only application paths and ordinary public source URLs become links."""
    value = str(value or '')
    if '\\' in value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        return ''
    if value.startswith('/') and not value.startswith('//'):
        return value
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ('https', 'http') and parsed.hostname and not parsed.username else ''
    except ValueError:
        return ''


def _item_view(context, draft, fields, nav):
    """Present validated findings without inventing source associations."""
    from .review_items import review_items
    by_id = {source['id']: source for source in context.get('evidence', [])}
    external = {source.get('id'): source for source in (draft.get('external_report') or {}).get('sources', []) if source.get('id')}
    by_id.update(external)
    target = context['target']
    target_sources = [source for source in context.get('evidence', []) if source.get('in_target') and safe_link(source.get('read_url'))]
    items = review_items(context, draft)
    for item in items:
        field = fields[item['field']]
        item.update(field_label=field['label'], current_display=field['current_display'],
                    proposed_display=field_display(field, item.get('value')), field_dom_id=field['dom_id'],
                    edit_url=field['review_edit_url'], evidence_items=[], leads=[],
                    draft_conflict=item['field'] in draft.get('changes', {}) and draft['changes'][item['field']]['value'] != item.get('value'))
        if item['status'] == 'saved' and 'saved_value' in item:
            item['proposed_display'] = field_display(field, item['saved_value'])
        item['status_label'] = {'pending': 'Needs review', 'accepted': 'Accepted for saving', 'rejected': 'Rejected',
                                'kept': 'Keeping current information', 'later': 'Deferred', 'saved': 'Saved'}.get(item['status'], item['status'])
        if item['status'] == 'saved' and item.get('edited'):
            item['status_label'] = 'Saved after editing'
        elif item['status'] == 'accepted' and item.get('edited'):
            item['status_label'] = 'Accepted after editing'
        for reference in item.get('evidence', []):
            source = by_id.get(reference.get('source_id'), {})
            # Legacy validated proposals carry their own source URLs; unresolved
            # findings may only point to sources actually retained with this run.
            historical = not source and (item.get('from_history') or item['status'] in ('rejected', 'kept', 'saved', 'later'))
            if not source and item['kind'] == 'question' and not historical:
                continue
            merged = {**source, **reference}
            href = safe_link(source.get('url') or source.get('read_url') or (merged.get('url') or merged.get('read_url') if item['kind'] == 'suggestion' or historical else ''))
            if not href:
                continue
            relationship = merged.get('relationship') or ('supporting_evidence' if item['kind'] == 'suggestion' else 'possible_lead')
            mismatch = merged.get('edition_match') is False and bool(merged.get('url'))
            if mismatch:
                relationship = 'possible_lead'
            if historical:
                relationship = 'possible_lead'
            merged.update(href=href, relationship_label={'supporting_evidence': 'Supporting evidence', 'possible_lead': 'Possible lead',
                          'checked_inconclusive': 'Checked but inconclusive'}.get(relationship, 'Possible lead'),
                          link_label=source.get('title') or merged.get('title') or ('Read the cited source for ' + field['label'].lower()),
                          link_help='Evidence from a previous review; recheck before relying on it.' if historical else 'Different edition; useful for comparison, not verification.' if mismatch else
                          'Inspect the source and confirm its scope.' if relationship == 'supporting_evidence' else 'May help resolve this question; it does not establish the answer.')
            item['evidence_items'].append(merged)
        item['leads'] = item['evidence_items'][:2]
        if not item['leads']:
            for source, position in zip(([target_sources[0], target_sources[-1]] if len(target_sources) > 1 else target_sources), ('opening', 'closing')):
                href = safe_link(source.get('read_url'))
                if any(lead['href'] == href for lead in item['leads']):
                    continue
                item['leads'].append({'href': href, 'link_label': 'Read the ' + position + ' source text for ' + target['title'],
                                     'relationship_label': 'Possible lead', 'link_help': 'Check headings and nearby credits; this link does not establish authorship.' if item['field'] in ('author', 'attribution', 'bibliography.authors', 'bibliography.editors') else 'Inspect the relevant wording and context before editing.'})
        reasons = list(dict.fromkeys(item.get('reasons', [])))
        item['reasons'] = reasons
        categories = set(item.get('categories', []))
        lower = ' '.join(reasons).lower()
        item['warnings'] = []
        if context['target']['scope'] == 'work' and ('edition_mismatch' in categories or ('isbn' in lower or 'edition' in lower) and any(word in lower for word in ('conflict', 'different', 'does not match', 'mismatch', 'another edition'))):
            item['warnings'].append('Edition match is unresolved. Check the ISBN and edition before verifying.')
        if 'conflicting_evidence' in categories:
            item['warnings'].append('Sources disagree. Inspect the conflict before choosing a value.')
        if 'incomplete_scope' in categories or any(phrase in lower for phrase in ('full content', 'throughout', 'multiple contributions', 'full scope')):
            item['warnings'].append('The evidence does not establish this value throughout the target. Check its full extent.')
    return items


def _item_groups(items, field_map):
    groups = []
    for item in items:
        related = {item['field'], *field_map[item['field']].get('related_fields', [])}
        matches = [group for group in groups if related.intersection(group['fields']) or any(item['field'] in field_map[key].get('related_fields', []) for key in group['fields'])]
        group = matches[0] if matches else {'items': [], 'fields': set(), 'reasons': [], 'evidence_items': [], 'leads': []}
        if not matches:
            groups.append(group)
        group['items'].append(item); group['fields'].add(item['field'])
        for extra in matches[1:]:
            group['items'].extend(extra['items']); group['fields'].update(extra['fields']); groups.remove(extra)
    for group in groups:
        group['warnings'] = list(dict.fromkeys(warning for item in group['items'] for warning in item['warnings']))
        group['id'] = 'review-group-' + hashlib.sha256('|'.join(sorted(group['fields'])).encode()).hexdigest()[:12]
        group['label'] = 'Related credits' if group['fields'].intersection({'author', 'attribution', 'bibliography.authors', 'bibliography.editors'}) and len(group['fields']) > 1 else 'Related titles' if len(group['fields']) > 1 else group['items'][0]['field_label']
        for item in group['items']:
            group['reasons'].extend(reason for reason in item['reasons'] if reason not in group['reasons'])
            for evidence in item['evidence_items']:
                if evidence not in group['evidence_items']:
                    group['evidence_items'].append(evidence)
            for lead in item['leads']:
                if not any(other['href'] == lead['href'] for other in group['leads']):
                    group['leads'].append(lead)
        group['leads'] = group['leads'][:2]
    return groups


def _integer(data, key, default=0):
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError('The review form has changed. Reload it before trying again.') from None
    if value < 0 or value > 1000000:
        raise ValueError('The review form has changed. Reload it before trying again.')
    return value


def _editable_proposal(context, draft, identity, field=''):
    from .review_items import review_items
    item = next((item for item in review_items(context, draft) if item['id'] == identity and item['kind'] == 'suggestion'), None)
    if not item or field and item['field'] != field or item.get('stale') or item.get('from_history') or draft.get('stale'):
        raise ValueError('This suggestion is no longer current. Review its source again or edit the field manually.')
    current = draft.get('changes', {}).get(item['field'])
    if current and current['value'] != item['value']:
        raise ValueError('Your existing draft differs from this suggestion and has been kept. Review that draft or explicitly replace it.')
    return item


def _proposal_evidence(item):
    lines = []
    for source in item.get('evidence', []):
        detail = source.get('quote') or source.get('summary') or ''
        location = source.get('url') or source.get('read_url') or source.get('source_id', '')
        lines.append(f'{detail} ({location})' if detail else location)
    return '\n'.join(lines)[:4000]


def _brief(value, limit=420):
    value = ' '.join(str(value or '').split())
    return value if len(value) <= limit else value[:limit].rsplit(' ', 1)[0] + '…'


def _guidance_view(context, draft, items, field_map):
    """Join the shared recommendations to safe, accessible presentation data."""
    from .review_guidance import recommendations
    item_map = {item['id']: item for item in items}
    sources = {source['id']: source for source in context.get('evidence', [])}
    sources.update({source['id']: source for source in (draft.get('external_report') or {}).get('sources', []) if source.get('id')})
    result = deepcopy(recommendations(context, draft))
    for rec in result:
        rec['items'] = [item_map[item_id] for item_id in rec.get('item_ids', []) if item_id in item_map]
        rec['change_items'] = [item for item in rec['items'] if item['kind'] == 'suggestion']
        rec['has_conflict'] = any(item.get('draft_conflict') for item in rec['change_items'])
        rec['stale'] = bool(rec.get('stale') or draft.get('stale') or any(item.get('stale') for item in rec['change_items']))
        rec['has_queued_changes'] = any(key in draft.get('changes', {}) for key in rec.get('fields', []))
        rec['reason_short'] = _brief(rec.get('reason'))
        if context['target']['scope'] != 'work' and 'isbn' in rec['reason_short'].lower():
            section_reason = next((reason for reason in rec.get('reasons', []) if 'isbn' not in reason.lower()), '')
            rec['reason_short'] = _brief(section_reason) or 'Use this section’s own text to check its title and contributor credits. The book’s identifier comparison is available in Book details.'
        rec['strength_label'] = {'well_supported': 'Well supported', 'likely': 'Likely', 'needs_evidence': 'Needs more evidence'}.get(rec.get('strength'), 'Needs more evidence')
        rec['strength_reason_short'] = _brief(rec.get('strength_reason'), 240)
        rec['evidence_items'] = []
        for reference in rec.get('evidence', []):
            source = sources.get(reference.get('source_id'), {})
            merged = {**source, **reference}
            href = safe_link(source.get('url') or source.get('read_url'))
            if href:
                relationship = reference.get('relationship', 'supporting_evidence')
                merged.update(href=href, link_label=source.get('title') or reference.get('title') or 'Read the source for this recommendation',
                              relationship_label={'supporting_evidence': 'Supporting evidence', 'possible_lead': 'Possible lead',
                                                  'checked_inconclusive': 'Checked but inconclusive'}.get(relationship, 'Possible lead'))
                rec['evidence_items'].append(merged)
        if not rec['evidence_items']:
            for item in rec['items']:
                rec['evidence_items'].extend(source for source in item['evidence_items'] if source not in rec['evidence_items'])
        rec['leads'] = []
        for item in rec['items']:
            for source in item['leads']:
                if not any(other['href'] == source['href'] for other in rec['leads']):
                    rec['leads'].append(source)
        rec['leads'] = rec['leads'][:2]
        rec['field_education'] = [field_map[key] for key in rec.get('fields', []) if key in field_map]
        if rec['kind'] == 'edition':
            rec['primary_action'] = 'keep'
            rec['action_label'] = 'Keep this ISBN'
        elif rec['change_items']:
            rec['primary_action'] = 'use'
            rec['action_label'] = rec.get('action_label') or ('Use these changes' if len(rec['change_items']) > 1 else field_map[rec['change_items'][0]['field']].get('action_label', 'Use this recommendation'))
        else:
            rec['primary_action'] = 'keep'
            rec['action_label'] = 'Keep this as it is' if rec['kind'] == 'keep' else 'Keep current information for now'
        for record in (rec.get('comparison') or {}).get('records', []):
            record['source_links'] = []
            for identity in record.get('source_ids', []):
                source = sources.get(identity, {})
                href = safe_link(source.get('url') or source.get('read_url'))
                if href:
                    record['source_links'].append({'href': href, 'label': source.get('title') or 'Read this statement in your copy'})
    return result


def register_review_routes(app, library, page, form, call, identifier):
    inflight = set()

    def navigation(data):
        return {'section_offset': _integer(data, 'section_offset'),
                'passage': identifier(data['passage']) if data.get('passage') else '',
                'step': str(data.get('step', ''))[:160]}

    def url(target_id, nav=None, anchor=''):
        params = {k: v for k, v in (nav or {}).items() if v}
        return '/review/' + quote(target_id, safe='') + ('?' + urlencode(params) if params else '') + ('#' + anchor if anchor else '')

    async def render(request, target_id, *, nav=None, message='', error='', error_field='', overrides=None, pending_override=None, status_code=200, saved_fields=None, action_item_id='', edit_field='', investigate_id='', edit_proposal='', step_id='', preview_selection=None):
        context = await call('review_context', identifier(target_id))
        draft = await call('review_draft', target_id)
        if pending_override is not None:
            draft = {**draft, 'changes': deepcopy(pending_override)}
        fields = deepcopy(context['fields'])
        field_map = {field['key']: field for field in fields}
        changes = draft.get('changes', {})
        prefill = None
        if edit_proposal:
            try:
                prefill = _editable_proposal(context, draft, edit_proposal, edit_field)
                edit_field = prefill['field']
            except ValueError as exc:
                error = str(exc)
        nav = nav or {'section_offset': 0, 'passage': ''}
        grouped = {}
        for index, field in enumerate(fields):
            key = field['key']
            field['dom_id'] = 'review-field-' + key.replace('.', '-')
            field['review_edit_url'] = url(target_id, {**nav, 'edit': key}, field['dom_id'])
            field['editor_open'] = key in (edit_field, error_field)
            change = changes.get(key)
            value = change['value'] if change else field.get('raw_value', field.get('value'))
            field['pending'] = change
            if prefill and prefill['field'] == key:
                value = prefill['value']
                change = {'value': value, 'evidence': _proposal_evidence(prefill), 'verified': False}
                field['proposal_prefill'] = prefill['id']
            field['current_display'] = field.get('display_value') or display_value(field.get('value'))
            field['edit_evidence'] = change.get('evidence', '') if change else ''
            field['edit_verified'] = bool(change and change.get('verified'))
            field['edit_value'] = '\n'.join(map(str, value or [])) if field.get('kind') == 'tags' else '' if value is None else str(value)
            if field.get('kind') == 'names':
                inherited_translator = key == 'bibliography.translators' and context['target']['scope'] != 'work'
                field['allow_inherit'] = inherited_translator
                field['edit_mode'] = 'inherit' if inherited_translator and value is None else 'unknown' if value is None else 'absent' if value == [] else 'names'
                names = value if isinstance(value, list) else field.get('value') or []
                field['edit_value'] = '\n'.join(contributor_name(person) for person in names)
                field['edit_shorts'] = '\n'.join(person.get('short', '') for person in names)
            if overrides and key in overrides:
                field.update(overrides[key])
            grouped.setdefault(field.get('group', 'Information'), []).append(field)
        proposals = []
        evidence_by_id = {item['id']: item for item in context.get('evidence', [])}
        for proposal in draft.get('suggestions', []):
            if proposal.get('field') not in field_map:
                continue
            item = deepcopy(proposal)
            item['field_label'] = field_map[item['field']]['label']
            item['current_display'] = field_map[item['field']]['current_display']
            item['proposed_display'] = field_display(field_map[item['field']], item.get('value'))
            item['evidence_items'] = []
            for evidence in item.get('evidence', []):
                source = evidence_by_id.get(evidence.get('source_id'), {})
                merged = {**source, **evidence}
                merged['href'] = safe_link(merged.get('url') or merged.get('read_url'))
                item['evidence_items'].append(merged)
            proposals.append(item)
        pending = []
        for key, change in changes.items():
            if key in field_map:
                pending.append({'field': field_map[key], 'change': change, 'proposed_display': field_display(field_map[key], change.get('value'))})
        items = _item_view(context, draft, field_map, nav)
        for item in items:
            item['proposal_edit_url'] = url(target_id, {**nav, 'edit': item['field'], 'proposal': item['id']}, field_map[item['field']]['dom_id'])
        item_by_field = {item['field']: item for item in items if item['kind'] == 'suggestion' and item['status'] == 'accepted'}
        for entry in pending:
            entry['item'] = item_by_field.get(entry['field']['key'])
            entry['evidence_items'] = entry['item']['evidence_items'] if entry['item'] else []
        active = [item for item in items if item['status'] == 'pending' or item['id'] == action_item_id]
        action_groups = _item_groups(active, field_map)
        for group in action_groups:
            group['investigate_url'] = url(target_id, {**nav, 'investigate': group['id']}, group['id'] + '-details')
        deferred = [item for item in items if item['status'] == 'later' and item['id'] != action_item_id]
        handled = [item for item in items if item['status'] in ('rejected', 'kept', 'saved') and item['id'] != action_item_id]
        guidance = _guidance_view(context, draft, items, field_map)
        if context['target']['scope'] != 'work':
            guidance = [rec for rec in guidance if rec['kind'] != 'edition']
        for rec in guidance:
            rec['url'] = url(target_id, {**nav, 'step': rec['id']}, 'guided-heading')
        guidance_map = {rec['id']: rec for rec in guidance}
        requested_step = step_id or nav.get('step') or (draft.get('progress') or {}).get('next_id', '')
        if requested_step in ('important', 'optional'):
            current = next((rec for rec in guidance if rec['priority'] == requested_step and rec['status'] == 'pending'), None)
            current = current or next((rec for rec in guidance if rec['priority'] == requested_step), None)
        elif requested_step == 'summary':
            current = None
        else:
            current = guidance_map.get(requested_step)
            current = current or next((rec for rec in guidance if rec['status'] == 'pending' and rec['priority'] != 'optional'), None)
        position = next((i for i, rec in enumerate(guidance) if current and rec['id'] == current['id']), -1)
        preview = None
        if current and current['change_items']:
            current['preview_open'] = preview_selection is not None
            current['selected_item_ids'] = preview_selection if preview_selection is not None else [item['id'] for item in current['change_items'] if not item['draft_conflict']]
            from .review_guidance import preview_recommendation
            try:
                preview = await run_in_threadpool(preview_recommendation, library, context, draft, current, selected_item_ids=current['selected_item_ids'])
            except (ValueError, LookupError):
                preview = {'changes': [], 'limitations': ['A citation preview is unavailable for this target. Your draft and evidence remain available.']}
        summary_url = url(target_id, {**nav, 'step': 'summary'}, 'pending-heading')
        last_rec = guidance_map.get((draft.get('progress') or {}).get('last_id'))
        undo_rec = last_rec if last_rec and (last_rec.get('decision') or {}).get('status') in ('accepted', 'kept', 'later') else None
        guided_step = current['id'] if current else 'summary'
        view_url = url(target_id, {**nav, 'step': guided_step}, 'guided-heading' if current else 'pending-heading')
        # Keep the same guided question when opening an editor without JS.
        for field in fields:
            field['review_edit_url'] = url(target_id, {**nav, 'step': guided_step, 'edit': field['key']}, field['dom_id'])
        for item in items:
            item['edit_url'] = field_map[item['field']]['review_edit_url']
            item['proposal_edit_url'] = url(target_id, {**nav, 'step': guided_step, 'edit': item['field'], 'proposal': item['id']}, field_map[item['field']]['dom_id'])
        from .review_jobs import generation_status
        generation = await run_in_threadpool(generation_status, library, target_id)
        return page(request, 'review.html', status_code=status_code, title='Review and edit information', nav='books',
                    review=context, target=context['target'], draft=draft, groups=grouped, field_map=field_map,
                    proposals=proposals, pending=pending, message=message, error=error, error_field=error_field,
                    saved_fields=saved_fields or [], navigation=nav, review_url=url(target_id, nav),
                    items=items, action_groups=action_groups, deferred=deferred, handled=handled, investigate_id=investigate_id,
                    action_item_id=action_item_id, edit_field=edit_field or error_field,
                    outstanding_count=sum(item['status'] in ('pending', 'later') for item in items),
                    deferred_count=sum(item['status'] == 'later' for item in items),
                    guidance=guidance, current_recommendation=current, guided_position=position + 1,
                    guided_previous=guidance[position - 1] if position > 0 else None,
                    guided_next=guidance[position + 1] if position >= 0 and position + 1 < len(guidance) else None,
                    guided_preview=preview, guided_step=guided_step, guided_view_url=view_url, summary_url=summary_url,
                    guided_autofocus=bool(message) and not error and not action_item_id,
                    undo_recommendation=undo_rec, summary_open=not current or pending_override is not None,
                    has_remaining_recommendations=any(rec['status'] in ('pending', 'partial', 'later') for rec in guidance),
                    important_guidance=[rec for rec in guidance if rec['priority'] != 'optional'],
                    optional_guidance=[rec for rec in guidance if rec['priority'] == 'optional'],
                    request_id=secrets.token_hex(20), busy=target_id in inflight or generation.get('running', False), safe_link=safe_link,
                    display_value=display_value)

    @app.get('/review/{target_id}')
    async def review_page(request: Request, target_id: str, work: str = Query('', max_length=160),
                          section_offset: int = Query(0, ge=0, le=100000), passage: str = Query('', max_length=160), edit: str = Query('', max_length=160), investigate: str = Query('', max_length=160), proposal: str = Query('', max_length=160), step: str = Query('', max_length=160)):
        identifier(target_id)
        if work:
            context = await call('review_context', target_id)
            if context['target']['work_id'] != identifier(work):
                raise HTTPException(404, 'That item is not part of this book.')
        return await render(request, target_id, nav={'section_offset': section_offset, 'passage': identifier(passage) if passage else ''}, edit_field=edit, investigate_id=investigate, edit_proposal=proposal, step_id=step)

    @app.post('/review/{target_id}/recommendation')
    async def decide_recommendation(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        recommendation_id = data.get('recommendation_id', '')
        try:
            selected = None
            if data.get('selection_present') == 'yes':
                count = _integer(data, 'item_count')
                if count > 80:
                    raise ValueError('Choose 80 or fewer changes in one recommendation.')
                selected = [data.get(f'item_{i}', '') for i in range(count) if data.get(f'selected_{i}') == 'yes']
            if data.get('preview') == 'yes':
                return await render(request, target_id, nav=nav, message='Preview updated. Nothing has been queued or saved.',
                                    step_id=recommendation_id, preview_selection=selected)
            result = await call('decide_recommendation', target_id, recommendation_id, data.get('decision', ''),
                                revision=data.get('revision', ''), expected_version=_integer(data, 'version'),
                                selected_item_ids=selected, replace=data.get('replace') == 'yes')
            return await render(request, target_id, nav=nav, message=result.get('message', 'Your choice is saved in this review.'),
                                step_id=result.get('next_recommendation_id') or 'summary')
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), step_id=recommendation_id, status_code=400)

    @app.post('/review/{target_id}/decision')
    async def decide(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        item_id = data.get('item_id', '')
        try:
            result = await call('decide_review', target_id, item_id, data.get('decision', ''),
                                revision=data.get('revision', ''), expected_version=_integer(data, 'version'), replace=data.get('replace') == 'yes')
            return await render(request, target_id, nav=nav, message=result.get('message', 'Review decision saved locally.'), action_item_id=item_id)
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), action_item_id=item_id, status_code=400)

    @app.get('/review/{target_id}/state')
    async def review_state(target_id: str):
        from .review_jobs import generation_status
        state = await run_in_threadpool(generation_status, library, identifier(target_id))
        return JSONResponse({'running': state['running'], 'status': state['status']})

    @app.post('/review/{target_id}/draft')
    async def save_draft(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        key = data.get('field', '')
        overrides = {key: {'edit_value': data.get('value', ''), 'edit_mode': data.get('mode', ''),
                           'edit_shorts': data.get('short_names', ''), 'edit_evidence': data.get('evidence', ''),
                           'edit_verified': data.get('verified') == 'yes', 'unsaved': True}}
        try:
            context = await call('review_context', target_id)
            fields = {item['key']: item for item in context['fields']}
            if key not in fields or not fields[key].get('editable', True):
                raise ValueError('Choose an editable field in this review.')
            draft = await call('review_draft', target_id)
            changes = deepcopy(draft.get('changes', {}))
            proposal = None
            if data.get('operation') == 'remove':
                changes.pop(key, None)
            else:
                from .review import draft_change
                proposal = _editable_proposal(context, draft, data['proposal_item_id'], key) if data.get('proposal_item_id') else None
                change = draft_change(fields[key], data.get('value', ''), changes.get(key), mode=data.get('mode', ''),
                                      short_names=data.get('short_names', ''), evidence=data.get('evidence', ''),
                                      verified=data.get('verified') == 'yes', proposal=proposal)
                changes[key] = change
            if proposal:
                result = await call('decide_review', target_id, proposal['id'], 'accept', edited_change=change,
                                    revision=data.get('revision', ''), expected_version=_integer(data, 'version'))
                return await render(request, target_id, nav=nav, message=result.get('message', 'Edited suggestion accepted for saving.'), action_item_id=proposal['id'])
            await call('save_review_draft', target_id, changes, revision=data.get('revision', ''), expected_version=_integer(data, 'version'))
            return await render(request, target_id, nav=nav, message='Draft saved locally. Review pending changes before saving them to the library.')
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), error_field=key, overrides=overrides, edit_proposal=data.get('proposal_item_id', ''), status_code=400)

    @app.post('/review/{target_id}/use-suggestion')
    async def use_suggestion(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        try:
            draft = await call('review_draft', target_id)
            suggestion = next((item for item in draft.get('suggestions', []) if item.get('id') == data.get('suggestion_id')), None)
            if not suggestion or suggestion.get('stale'):
                raise ValueError('This suggestion is no longer current. Request a new review or edit the field manually.')
            evidence = []
            for source in suggestion.get('evidence', []):
                detail = source.get('quote') or source.get('summary') or ''
                location = source.get('url') or source.get('read_url') or source.get('source_id', '')
                evidence.append(f'{detail} ({location})' if detail else location)
            from .review_items import review_items, item_id
            context = await call('review_context', target_id)
            identity = item_id(target_id, 'suggestion', suggestion['field'], suggestion['value'])
            item = next(item for item in review_items(context, draft) if item['id'] == identity)
            result = await call('decide_review', target_id, item['id'], 'accept', revision=data.get('revision', ''), expected_version=_integer(data, 'version'))
            return await render(request, target_id, nav=nav, message=result.get('message', 'Suggestion accepted for saving.'), action_item_id=item['id'])
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), status_code=400)

    @app.get('/review/{target_id}/{action}')
    async def reopen_action(target_id: str, action: str, section_offset: int = Query(0, ge=0, le=100000), passage: str = Query('', max_length=160)):
        if action not in {'draft', 'use-suggestion', 'draft-evidence', 'linked-title', 'apply', 'discard', 'rebase', 'suggest', 'decision', 'recommendation'}:
            raise HTTPException(404, 'That review action is not available.')
        identifier(target_id)
        return RedirectResponse(url(target_id, {'section_offset': section_offset, 'passage': identifier(passage) if passage else ''}, 'ai-review' if action == 'suggest' else 'pending-review'), status_code=303)

    @app.post('/review/{target_id}/draft-evidence')
    async def save_pending_evidence(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        try:
            draft = await call('review_draft', target_id)
            changes = deepcopy(draft.get('changes', {}))
            count = _integer(data, 'count')
            if count > 64:
                raise ValueError('Please review 64 or fewer changes at once.')
            for i in range(count):
                key = data.get(f'field_{i}', '')
                if key not in changes:
                    raise ValueError('The pending changes have changed. Reload this review before saving the source notes.')
                if f'evidence_{i}' in data:
                    changes[key]['evidence'] = data.get(f'evidence_{i}', '').strip()
                if f'evidence_{i}' in data or f'verified_{i}' in data:
                    changes[key]['verified'] = data.get(f'verified_{i}') == 'yes'
            await call('save_review_draft', target_id, changes, revision=data.get('revision', ''), expected_version=_integer(data, 'version'))
            return await render(request, target_id, nav=nav, message='Source notes and verification choices saved in your local draft.')
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), pending_override=changes if 'changes' in locals() else None, status_code=400)

    @app.post('/review/{target_id}/linked-title')
    async def linked_title(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        try:
            context = await call('review_context', target_id)
            fields = {item['key']: item for item in context['fields']}
            source_key, target_key = data.get('source_field', ''), data.get('field', '')
            if {source_key, target_key} != {'title', 'bibliography.title'} or target_key not in fields or not fields[target_key].get('editable', True):
                raise ValueError('Choose related title fields within this review.')
            draft = await call('review_draft', target_id)
            source = draft.get('changes', {}).get(source_key)
            if not source or not isinstance(source.get('value'), str):
                raise ValueError('Save the proposed title in your draft first.')
            changes = deepcopy(draft['changes'])
            if target_key in changes and changes[target_key]['value'] != source['value']:
                raise ValueError('The related title already has a different draft. Review or edit that draft before replacing it.')
            changes[target_key] = {**deepcopy(source), 'verified': False}
            await call('save_review_draft', target_id, changes, revision=data.get('revision', ''), expected_version=_integer(data, 'version'))
            return await render(request, target_id, nav=nav, message='Related title added to your draft. Review each title’s effect and evidence before saving.')
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), status_code=400)

    @app.post('/review/{target_id}/apply')
    async def apply(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        try:
            draft = await call('review_draft', target_id)
            changes = deepcopy(draft.get('changes', {}))
            selected = []
            count = _integer(data, 'count')
            if count > 64:
                raise ValueError('Please review 64 or fewer changes at once.')
            for i in range(count):
                key = data.get(f'field_{i}', '')
                if key not in changes:
                    # An identical successful request may already have removed the draft.
                    continue
                # Compact final forms retain server-side evidence and verification.
                # Legacy forms may still explicitly submit those controls.
                if f'evidence_{i}' in data:
                    changes[key]['evidence'] = data.get(f'evidence_{i}', '').strip()
                if f'evidence_{i}' in data or f'verified_{i}' in data:
                    changes[key]['verified'] = data.get(f'verified_{i}') == 'yes'
                if data.get(f'selected_{i}') == 'yes':
                    selected.append(key)
            result = await call('apply_review', target_id, selected, revision=data.get('revision', ''),
                                expected_version=_integer(data, 'version'), request_id=data.get('request_id', ''), changes=changes)
            message = 'No information changed. Existing verification and evidence were preserved.'
            if result.get('saved'):
                message = 'Selected corrections saved to the library.'
                if result.get('refresh_pending'):
                    message += ' Search refresh is pending; your corrections are saved. Retry the refresh from Library status.'
            return await render(request, target_id, nav=nav, message=message, saved_fields=result.get('fields', []))
        except ValueError as exc:
            # Keep submitted evidence visible, but do not overwrite a newer persisted draft.
            response = await render(request, target_id, nav=nav, error=str(exc), pending_override=changes if 'changes' in locals() else None, status_code=400)
            return response

    @app.post('/review/{target_id}/discard')
    async def discard(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        try:
            await call('discard_review_draft', target_id, expected_version=_integer(data, 'version'))
            return await render(request, target_id, nav=nav, message='Unsaved changes discarded. Review decisions and saved library information are retained.')
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), status_code=400)

    @app.post('/review/{target_id}/rebase')
    async def rebase(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        try:
            await call('rebase_review_draft', target_id, expected_version=_integer(data, 'version'))
            return await render(request, target_id, nav=nav, message='Draft rechecked against current information. Review the differences and verify each field again before saving.')
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), status_code=400)

    @app.post('/review/{target_id}/suggest')
    async def suggest(request: Request, target_id: str):
        data = await form(request)
        identifier(target_id)
        nav = navigation(data)
        try:
            context = await call('review_context', target_id)
            if context['revision'] != data.get('revision'):
                raise ValueError('The information changed while this page was open. Reload and review it before requesting suggestions.')
            if target_id in inflight:
                raise ValueError('An AI review is already running for this item. Return to this page shortly to see its results.')
            from .review_jobs import begin_generation, finish_generation
            generation_id = data.get('generation_request_id', data.get('request_id', ''))
            generation = await run_in_threadpool(begin_generation, library, target_id, context['revision'], generation_id)
            if generation.get('replay'):
                return await render(request, target_id, nav=nav, step_id='important', message='This AI request was already handled. Its saved results are shown below; no new paid request was made.')
            inflight.add(target_id)
            try:
                from .review_ai import suggest as generate
                result = await run_in_threadpool(generate, library, context)
                await call('store_review_suggestions', target_id, result, revision=context['revision'])
                await run_in_threadpool(finish_generation, library, target_id, generation_id, status=result.get('status', 'complete'))
            except BaseException:
                await run_in_threadpool(finish_generation, library, target_id, generation_id, status='interrupted')
                raise
            finally:
                inflight.discard(target_id)
            return await render(request, target_id, nav=nav, step_id='important', message='AI review finished. Suggestions are separate from saved information; choose which ones to use.')
        except ValueError as exc:
            return await render(request, target_id, nav=nav, error=str(exc), status_code=400)
