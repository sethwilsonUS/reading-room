"""Library-only cost reservations. Unknown outcomes remain charged conservatively."""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
from .storage import writer, jsonl, append_jsonl, now, uid
from .public_config import budget_settings, require_budget

RATE_PER_MILLION=Decimal('0.13')  # text-embedding-3-large; verified 2026-10-30
CAPS={'pilot':Decimal('10'),'initial':Decimal('50'),'monthly':Decimal('25')}

# Versioned separately from the frozen embedding experiment. This is a
# conservative reservation, not a claim about a provider-enforced price cap.
REVIEW_RATE_VERSION = 'gpt-6-luna-standard-2026-11-03'
REVIEW_MODEL = 'gpt-6-luna'
REVIEW_RESERVE = Decimal('0.75')
REVIEW_STAGE_HOLDS = {'local': Decimal('0.004'), 'web': Decimal('0.743'), 'normalize': Decimal('0.003')}


def review_usage_cost(usage, *, searches=0):
    """Price complete Responses usage; missing counters remain uncertain."""
    if not isinstance(usage, dict) or not isinstance(searches, int) or not 0 <= searches <= 2:
        raise ValueError('Complete review usage is required.')
    details = usage.get('input_tokens_details')
    if not isinstance(details, dict):
        raise ValueError('Review cache usage is missing.')
    counts = [usage.get('input_tokens'), usage.get('output_tokens'), details.get('cached_tokens'), details.get('cache_write_tokens')]
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError('Review token usage is incomplete.')
    inputs, outputs, cached, writes = counts
    if cached + writes > inputs:
        raise ValueError('Review token usage is inconsistent.')
    write_details = details.get('cache_write_tokens_details')
    if write_details is not None:
        if not isinstance(write_details, dict) or write_details.get('ttl_12h_tokens', 0) != 0 or write_details.get('ttl_30m_tokens') != writes:
            raise ValueError('Unexpected review cache lifetime.')
    long_context = inputs > 272_000
    input_multiplier = Decimal('2') if long_context else Decimal('1')
    output_multiplier = Decimal('1.5') if long_context else Decimal('1')
    dollars = ((Decimal(inputs-cached-writes)*Decimal('0.10') + Decimal(cached)*Decimal('0.01') + Decimal(writes)*Decimal('0.125'))*input_multiplier
               + Decimal(outputs)*Decimal('0.50')*output_multiplier)/Decimal(1_000_000)
    return dollars + Decimal(searches)*Decimal('0.01')

class BudgetExceeded(RuntimeError): pass

class Budget:
    def __init__(self,state): self.state=state; self.path=state/'costs.jsonl'
    def _settings(self):
        return budget_settings(self.state)
    def _caps(self):
        settings = self._settings()
        return {**CAPS, 'initial': Decimal(settings['initial_usd']), 'monthly': Decimal(settings['monthly_usd'])}
    def period(self,scope):
        return datetime.now(ZoneInfo(self._settings()['timezone'])).strftime('%Y-%m') if scope=='monthly' else scope
    def _spent(self,scope,period):
        reservations={}; settlements={}
        for e in jsonl(self.path):
            if e.get('scope')!=scope or e.get('period')!=period: continue
            if e['kind']=='reserve': reservations[e['request_id']]=Decimal(e['dollars'])
            if e['kind']=='settle': settlements[e['request_id']]=Decimal(e['dollars'])
        return sum((settlements.get(k,v) for k,v in reservations.items()),Decimal('0'))
    def reserve(self,tokens,scope='pilot',initial_approved=False):
        if not isinstance(tokens,int) or tokens<0: raise ValueError('Token reservation must be a nonnegative integer.')
        if scope not in CAPS: raise ValueError('Unknown budget scope.')
        if scope=='initial' and not initial_approved: raise BudgetExceeded('Full build awaits an explicit engine choice and authorization.')
        if scope != 'pilot': require_budget(self.state)
        estimate=Decimal(int(tokens))*RATE_PER_MILLION/Decimal(1_000_000)
        period=self.period(scope)
        with writer(self.state):
            spent=self._spent(scope,period)
            if spent+estimate>self._caps()[scope]: raise BudgetExceeded(f'{scope} ceiling would be exceeded; exact search remains available.')
            rid=uid('request'); append_jsonl(self.path,{'kind':'reserve','request_id':rid,'scope':scope,'period':period,'tokens':int(tokens),'dollars':str(estimate),'model':'text-embedding-3-large','created_at':now()})
        return {'id':rid,'scope':scope,'period':period,'reserved_tokens':int(tokens)}
    def settle(self,reservation,actual_tokens):
        if not isinstance(actual_tokens,int) or actual_tokens<0: raise ValueError('Actual tokens must be a nonnegative integer.')
        dollars=Decimal(int(actual_tokens))*RATE_PER_MILLION/Decimal(1_000_000)
        with writer(self.state):
            events=jsonl(self.path)
            if not any(e['kind']=='reserve' and e['request_id']==reservation['id'] for e in events): raise ValueError('Unknown reservation.')
            settled=next((e for e in events if e['kind']=='settle' and e['request_id']==reservation['id']),None)
            if settled:
                if settled['tokens']==actual_tokens: return
                raise ValueError('Conflicting settlement for an already settled reservation.')
            append_jsonl(self.path,{'kind':'settle','request_id':reservation['id'],'scope':reservation['scope'],'period':reservation['period'],'tokens':int(actual_tokens),'dollars':str(dollars),'created_at':now()})

    def reserve_review(self, stages=None):
        """Reserve all possible stages atomically against the existing monthly cap."""
        require_budget(self.state)
        if stages is None:
            stages = list(REVIEW_STAGE_HOLDS)
        if not isinstance(stages, (list, tuple, set)) or not stages or any(stage not in REVIEW_STAGE_HOLDS for stage in stages):
            raise ValueError('Choose the remaining supported review stages.')
        holds = {stage: REVIEW_STAGE_HOLDS[stage] for stage in REVIEW_STAGE_HOLDS if stage in stages}
        reserve = sum(holds.values(), Decimal('0'))
        period = self.period('monthly')
        with writer(self.state):
            if self._spent('monthly', period) + reserve > self._caps()['monthly']:
                raise BudgetExceeded('The monthly allowance cannot reserve this AI review. Manual editing remains available.')
            rid = uid('review_request')
            append_jsonl(self.path, {'kind': 'reserve', 'request_id': rid, 'scope': 'monthly', 'period': period,
                'dollars': str(reserve), 'model': REVIEW_MODEL, 'rate_version': REVIEW_RATE_VERSION,
                'purpose': 'correction_review', 'stage_holds': {key: str(value) for key, value in holds.items()}, 'created_at': now()})
        return {'id': rid, 'scope': 'monthly', 'period': period, 'reserved_dollars': str(reserve)}

    def begin_review_stage(self, reservation, stage, *, before_start=None):
        if stage not in REVIEW_STAGE_HOLDS:
            raise ValueError('Unknown review stage.')
        with writer(self.state):
            events = jsonl(self.path)
            original = next((event for event in events if event['kind'] == 'reserve' and event['request_id'] == reservation['id'] and event.get('purpose') == 'correction_review'), None)
            if original is None or original['period'] != reservation['period']:
                raise ValueError('Unknown review reservation.')
            if stage not in original.get('stage_holds', REVIEW_STAGE_HOLDS):
                raise ValueError('This stage is not part of the remaining review reservation.')
            prior = [event for event in events if event.get('request_id') == reservation['id']]
            if any(event['kind'] == 'settle' or (event['kind'] == 'review_stage_started' and event.get('stage') == stage) for event in prior):
                raise ValueError('This review stage has already started or finished; it cannot be retried.')
            if any(event['kind'] == 'review_stage_settled' and Decimal(event['dollars']) > REVIEW_STAGE_HOLDS[event['stage']] for event in prior):
                raise BudgetExceeded('A review exceeded its conservative reservation; further paid stages are stopped.')
            if before_start is not None:
                # Recheck after acquiring the writer: an index publication can
                # hold it longer than the caller's foreground request window.
                before_start()
            append_jsonl(self.path, {'kind': 'review_stage_started', 'request_id': reservation['id'], 'stage': stage, 'created_at': now()})

    def check_review_continuation(self, reservations):
        """Carry stage-overrun protection across clean-pause reservations."""
        request_ids = {item['id'] for item in reservations}
        with writer(self.state):
            events = [event for event in jsonl(self.path) if event.get('request_id') in request_ids]
            if any(event['kind'] == 'review_stage_settled' and
                   Decimal(event['dollars']) > REVIEW_STAGE_HOLDS[event['stage']] for event in events):
                raise BudgetExceeded('A previous review stage exceeded its conservative reservation; further paid stages are stopped.')

    def settle_review_stage(self, reservation, stage, usage, *, searches=0):
        dollars = review_usage_cost(usage, searches=searches)
        with writer(self.state):
            events = jsonl(self.path)
            prior = [event for event in events if event.get('request_id') == reservation['id']]
            if not any(event['kind'] == 'review_stage_started' and event.get('stage') == stage for event in prior):
                raise ValueError('Review stage was not reserved and started.')
            old = next((event for event in prior if event['kind'] == 'review_stage_settled' and event['stage'] == stage), None)
            if old:
                if old['usage'] == usage and old['searches'] == searches:
                    return
                raise ValueError('Conflicting review usage settlement.')
            if any(event['kind'] == 'settle' for event in prior):
                raise ValueError('Review accounting has already finished.')
            append_jsonl(self.path, {'kind': 'review_stage_settled', 'request_id': reservation['id'], 'stage': stage,
                'dollars': str(dollars), 'usage': usage, 'searches': searches, 'rate_version': REVIEW_RATE_VERSION, 'created_at': now()})

    def finish_review(self, reservation):
        """Release unused stages, preserving the hold for every unknown attempt."""
        with writer(self.state):
            events = [event for event in jsonl(self.path) if event.get('request_id') == reservation['id']]
            original = next((event for event in events if event['kind'] == 'reserve' and event.get('purpose') == 'correction_review'), None)
            if original is None:
                raise ValueError('Unknown review reservation.')
            old = next((event for event in events if event['kind'] == 'settle'), None)
            if old:
                return old
            started = {event['stage'] for event in events if event['kind'] == 'review_stage_started'}
            settled = {event['stage']: event for event in events if event['kind'] == 'review_stage_settled'}
            unknown = sorted(started-set(settled))
            dollars = sum((Decimal(event['dollars']) for event in settled.values()), Decimal('0')) + sum((REVIEW_STAGE_HOLDS[stage] for stage in unknown), Decimal('0'))
            result = {'kind': 'settle', 'request_id': reservation['id'], 'scope': original['scope'], 'period': original['period'],
                'dollars': str(dollars), 'tokens': sum(event['usage']['input_tokens']+event['usage']['output_tokens'] for event in settled.values()),
                'uncertain': bool(unknown), 'uncertain_stages': unknown, 'rate_version': REVIEW_RATE_VERSION, 'created_at': now()}
            append_jsonl(self.path, result)
            return result

    def report(self):
        events=jsonl(self.path)
        uncertain = ({e['request_id'] for e in events if e['kind']=='reserve'}-{e['request_id'] for e in events if e['kind']=='settle'}) | {e['request_id'] for e in events if e['kind']=='settle' and e.get('uncertain')}
        zone = self._settings()['timezone']
        local_now = datetime.now(ZoneInfo(zone))
        reset = datetime(local_now.year + (local_now.month == 12), local_now.month % 12 + 1, 1, tzinfo=ZoneInfo(zone))
        return {'ceilings':{k:float(v) for k,v in self._caps().items()},'spent_or_reserved':{k:float(self._spent(k,self.period(k))) for k in CAPS},'requests':sum(e['kind']=='reserve' for e in events),'actual_tokens':sum(e.get('tokens',0) for e in events if e['kind']=='settle'),'uncertain_reservations':len(uncertain),'monthly_timezone':zone, 'monthly_resets_at': reset.isoformat()}
