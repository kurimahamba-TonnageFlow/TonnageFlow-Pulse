"""One recorded-state snapshot, reusing Pulse's fixed-hour reconciliation."""
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from . import database as db, pulse_calculations as calc, hourly_reports
from .factory_time import TimeWindow, factory_day_start, factory_date_of, clock_hour_start, current_shift_window, FACTORY_TZ


def configured_thresholds():
    amber=float(os.getenv('PULSE_DASHBOARD_AMBER_PERCENT','45'))
    green=float(os.getenv('PULSE_DASHBOARD_GREEN_PERCENT','65'))
    if not 0 < amber < green <= 100:
        raise ValueError('Dashboard thresholds must satisfy 0 < amber < green <= 100')
    return dict(amber=amber,green=green)


def reporting_week(day):
    return TimeWindow('production_week', f'Production week from {day.isoformat()}', factory_day_start(day), factory_day_start(day+timedelta(days=7)))


def reporting_start(now, targets):
    today=factory_date_of(now)
    effective=[t['week_start'] for t in targets if t['scope']=='site' and t['week_start']<=today]
    weekday=max(effective).weekday() if effective else 0
    return today-timedelta(days=(today.weekday()-weekday)%7)


def load_snapshot(now, week_start=None):
    """A fixed number of bulk queries, one read-only repeatable-read snapshot."""
    recent=min(clock_hour_start(now)-timedelta(hours=8), current_shift_window(now).start)
    seven=now-timedelta(days=7)
    with db.get_database_connection() as connection:
        with connection.cursor(row_factory=db.dict_row) as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            c.execute('SET LOCAL statement_timeout=15000')
            c.execute('SELECT id,name FROM public.production_lines WHERE active ORDER BY display_order,name')
            lines=c.fetchall()
            c.execute("SELECT * FROM public.weekly_tonnage_targets WHERE scope='site' ORDER BY week_start DESC")
            targets=c.fetchall()
            week=reporting_week(week_start or reporting_start(now,targets))
            c.execute(f"""SELECT pr.* FROM public.production_runs pr WHERE {db._TEST_DATA_EXCLUSION_SQL}
                AND (pr.status='Active' OR (pr.started_at<%s AND (pr.finished_at IS NULL OR pr.finished_at>%s))
                OR (pr.started_at<%s AND (pr.finished_at IS NULL OR pr.finished_at>%s))) ORDER BY pr.started_at""", (now,seven,week.end,week.start))
            runs=c.fetchall();ids=[r['id'] for r in runs]
            c.execute("""SELECT * FROM public.hourly_updates WHERE production_run_id=ANY(%s)
                AND ((hour_start>=%s AND hour_start<%s) OR (shift_window_start>=%s AND shift_window_start<%s)
                OR id IN (SELECT DISTINCT ON (production_run_id) id FROM public.hourly_updates
                  WHERE production_run_id=ANY(%s) ORDER BY production_run_id,period_ended_at DESC,id DESC)) ORDER BY id""",(ids,recent,now,week.start,week.end,ids))
            readings=c.fetchall()
            c.execute('SELECT * FROM public.run_operating_speed_changes WHERE production_run_id=ANY(%s) ORDER BY effective_at,id',(ids,))
            operating=c.fetchall()
            c.execute('SELECT * FROM public.planned_downtime_events WHERE production_run_id=ANY(%s) AND started_at<%s AND (ended_at IS NULL OR ended_at>%s)',(ids,now,seven))
            planned=c.fetchall()
            c.execute(f"""SELECT de.*,pr.production_line,b.fault_category,
                (SELECT eu.repair_classification FROM public.engineering_updates eu WHERE eu.downtime_event_id=de.id AND eu.repair_classification IS NOT NULL ORDER BY eu.id DESC LIMIT 1) AS repair_classification
                FROM public.downtime_events de JOIN public.production_runs pr ON pr.id=de.production_run_id LEFT JOIN public.buttons b ON b.id=de.button_id
                WHERE {db._TEST_DATA_EXCLUSION_SQL} AND de.opened_at<%s AND (de.resolved_at IS NULL OR de.resolved_at>%s OR (de.engineer_called AND de.engineering_status IN ('Not Started','In Progress','Ongoing')))""",(now,seven))
            faults=c.fetchall()
            c.execute("""SELECT ls.*,pr.standard_speed_ppm AS reference_speed_ppm,co.workflow,
                q.engineer AS changeover_engineer,q.ready_at AS engineering_ready_at,q.accepted_at AS engineering_accepted_at
                FROM public.line_stoppages ls LEFT JOIN public.production_runs pr ON pr.id=ls.previous_production_run_id
                LEFT JOIN public.changeovers co ON co.line_stoppage_id=ls.id
                LEFT JOIN public.casepacker_requests q ON q.line_stoppage_id=ls.id
                WHERE ls.production_line NOT ILIKE 'TEST-%%' AND ls.started_at<%s AND (ls.ended_at IS NULL OR ls.ended_at>%s)""",(now,seven))
            stoppages=c.fetchall()
    return dict(lines=[l['name'] for l in lines],runs=runs,readings=readings,operating_changes=operating,speed_changes=[],planned=planned,faults=faults,stoppages=stoppages,targets=targets),week


def number(v):
    return float(v) if v is not None else None


def weekly_production(data,week,now):
    runs={r['id']:r for r in data['runs']}
    total=calc.ZERO;count=0;excluded=0;edge=0
    # Fixed-hour rows are unique in PostgreSQL. Keep the latest version if a fixture/read source repeats one.
    unique={ (r['production_run_id'],r.get('hour_start') or r['id']):r for r in sorted(data['readings'],key=lambda r:r['id']) }
    for row in unique.values():
        membership=row.get('shift_window_start')
        if membership is None or not week.start<=membership<week.end:
            continue
        run=runs.get(row['production_run_id'])
        if not run or row.get('period_ended_at') is None or row['period_ended_at']>now:
            excluded+=1;continue
        try:
            cfg=calc.PackConfig.from_row(run)
            pallets=calc.to_decimal(row['pallets_completed'])
            if pallets is None or pallets<0: raise ValueError('Invalid quantity')
            total+=cfg.packs_to_tonnes(cfg.pallets_to_packs(pallets));count+=1
            if row['period_started_at']<week.start or row['period_ended_at']>week.end:edge+=1
        except (ValueError,TypeError,KeyError):excluded+=1
    target=next((t for t in data['targets'] if t['week_start']==week.start.astimezone(FACTORY_TZ).date()),None)
    expected=calc.to_decimal(target['target_tonnes']) if target else None
    return dict(week_start=week.start.astimezone(FACTORY_TZ).date(),window=week.to_api(),site='This factory',target_tonnes=number(expected),actual_tonnes=number(total) if count else None,progress_percent=number(calc.percent(total,expected)) if count and expected else None,confirmed_readings=count,excluded_readings=excluded,boundary_readings=edge,notes=target.get('notes','') if target else '',set_by=target.get('set_by') if target else None,method='Confirmed hourly pallets x the run’s historical cases/pallet x packs/case x kg/pack. Whole readings belong to their recorded operational shift. Produced, not dispatched tonnes.')


def line_intervals(data,line,start,end):
    runs=[r for r in data['runs'] if r['production_line']==line]
    run_ids={r['id'] for r in runs}
    stops=[s for s in data['stoppages'] if s['production_line']==line]
    run_cover=[(r['started_at'],r['finished_at'] or end) for r in runs]
    scheduled=calc.merge_intervals(run_cover+[(s['started_at'],s['ended_at'] or end) for s in stops if s['kind']!='not_scheduled'])
    coverage=calc.intersect_intervals(scheduled,[(start,end)])
    planned=[(p['started_at'],p['ended_at'] or end,p['reason'],('planned',p['id'])) for p in data['planned'] if p['production_run_id'] in run_ids]
    planned += [(s['started_at'],s['ended_at'] or end,s['kind'],('stop',s['id'])) for s in stops if s['kind'] in ('changeover','handover')]
    faults=[f for f in data['faults'] if f['production_line']==line]
    unplanned=[(f['opened_at'],f['resolved_at'] or end,f.get('fault_category') or f.get('repair_classification') or 'Other Unplanned',('fault',f['id'])) for f in faults]
    unplanned += [(s['started_at'],s['ended_at'] or end,'Other Unplanned',('stop',s['id'])) for s in stops if s['kind'] in ('other','restart_delay')]
    planned_union=calc.intersect_intervals([(a,b) for a,b,_,_ in planned],coverage)
    unplanned_union=calc.subtract_intervals(calc.intersect_intervals([(a,b) for a,b,_,_ in unplanned],coverage),planned_union)
    counts=lambda events:len({key for a,b,_,key in events if calc.intersect_intervals([(a,b)],coverage)})
    changeovers=calc.intersect_intervals([(s['started_at'],s['ended_at'] or end) for s in stops if s['kind']=='changeover'],coverage)
    groups={};allocated=[]
    for a,b,category,key in sorted(unplanned,key=lambda e:(e[0],str(e[3]))):
        pieces=calc.subtract_intervals(calc.intersect_intervals([(a,b)],unplanned_union),allocated)
        if not pieces:continue
        value=groups.setdefault(category,{'minutes':calc.ZERO,'events':set(),'records':[]})
        value['minutes']+=calc.total_minutes(pieces);value['events'].add(key);allocated+=pieces
        source=next((f for f in faults if key==('fault',f['id'])),None)
        value['records'].append(dict(id=key[1],source=key[0],machine=source['machine'] if source else 'Line stop',reason=source['reason'] if source else category,minutes=number(calc.total_minutes(pieces))))
    return dict(planned_minutes=number(calc.total_minutes(planned_union)),unplanned_minutes=number(calc.total_minutes(unplanned_union)),planned_events=counts(planned),unplanned_events=len({key for a,b,_,key in unplanned if calc.intersect_intervals([(a,b)],unplanned_union)}),changeover_minutes=number(calc.total_minutes(changeovers)),breakdowns=[{'category':k,'minutes':number(v['minutes']),'events':len(v['events']),'records':v['records']} for k,v in sorted(groups.items())])


def current_line(data,line,now):
    active=next((r for r in reversed(data['runs']) if r['production_line']==line and r['status']=='Active'),None)
    stops=[s for s in data['stoppages'] if s['production_line']==line and s['ended_at'] is None]
    stop=max(stops,key=lambda s:s['started_at']) if stops else None
    faults=[f for f in data['faults'] if f['production_line']==line and f['production_status']=='Ongoing' and f['resolved_at'] is None]
    planned=[p for p in data['planned'] if active and p['production_run_id']==active['id'] and p['ended_at'] is None]
    readings=[r for r in data['readings'] if active and r['production_run_id']==active['id'] and r.get('period_ended_at') and r['period_ended_at']<=now]
    last=max(readings,key=lambda r:r['period_ended_at']) if readings else None
    line_ids={r['id'] for r in data['runs'] if r['production_line']==line}
    latest_production=calc.latest(*(r.get('period_ended_at') for r in data['readings'] if r['production_run_id'] in line_ids and r.get('period_ended_at') and r['period_ended_at']<=now))
    recorded=last['period_ended_at'] if last else None
    status='idle';label='No active run';tone='neutral'
    if stop:
        status={'changeover':'changeover','not_scheduled':'not_scheduled'}.get(stop['kind'],'stopped');label={'changeover':'Changeover','not_scheduled':'Not Scheduled'}.get(stop['kind'],stop['kind'].replace('_',' ').title());tone='warning' if status=='changeover' else 'neutral' if status=='not_scheduled' else 'danger';recorded=stop['started_at']
    elif planned:status='stopped';label='Planned stop';tone='warning';recorded=max(p['started_at'] for p in planned)
    elif faults:status='stopped';label='Production stopped';tone='danger';recorded=max(f['opened_at'] for f in faults)
    elif active:
        status='unknown';label='Run active - status unconfirmed'
        if last and (now-last['period_ended_at']).total_seconds()<=75*60 and last['pallets_completed']>0:
            status='running';label='Running at last report';tone='success'
    job=None
    if active:
        completed=calc.to_decimal(active.get('total_pallets_completed'))
        remaining=calc.to_decimal(active.get('pallets_remaining'))
        total=calc.to_decimal(active.get('starting_pallets_remaining'))
        job=dict(run_id=active['id'],product=active['product'],customer=active['customer'],format=active.get('format') or active.get('pack_type'),pack_weight_kg=number(active.get('pack_weight_kg')),speed_ppm=number(active.get('standard_speed_ppm')),completed_pallets=number(completed),remaining_pallets=number(remaining),progress_percent=number(calc.percent(completed,total)) if total else None,estimate_hours=None,estimate_status='Estimate unavailable')
        # No speculative changeover ETA: capture is periodic, and future job is unconfirmed.
    changeover=None
    if stop and stop['kind']=='changeover':
        wf=stop.get('workflow') or {}
        changeover=dict(id=stop['id'],kind=wf.get('kind','Unclassified'),elapsed_minutes=number(calc.minutes_between(stop['started_at'],now)),physical_complete=stop.get('physical_ended_at') is not None,qa_status=wf.get('qa_status','Not recorded'),engineering_ready=stop.get('engineering_ready_at') is not None,engineer=stop.get('changeover_engineer'))
    return dict(name=line,status=status,status_label=label,tone=tone,status_recorded_at=recorded,last_production_at=latest_production,job=job,changeover=changeover)


def build_snapshot(data,week,now):
    shift=current_shift_window(now)
    end=clock_hour_start(now);start=min(shift.start,end-timedelta(hours=2))
    window=TimeWindow('completed_hours','Completed clock hours',start,end)
    report=hourly_reports.build_hourly_report(data,window,now)
    by_line={l['production_line']:l for l in report['lines']}
    lines=[]
    for name in data['lines']:
        state=current_line(data,name,now)
        slots=by_line[name]['hours']
        state.update(last_hour=slots[-1] if slots else None,trend=[h for h in slots if h['hour_start']>=shift.start])
        lines.append(state)
    def average(index):
        values=[(name,l['hours'][index]['line']) for name,l in by_line.items() if len(l['hours'])>=abs(index)]
        eligible=[(name,v) for name,v in values if v['output_vs_target_percent'] is not None and v['status'] in ('reported','stopped') and v['unaccounted_minutes']==0]
        total_target=sum((calc.to_decimal(v['target_packs']) for _,v in eligible),calc.ZERO)
        total_actual=sum((calc.to_decimal(v['actual_packs']) for _,v in eligible),calc.ZERO)
        return number(calc.percent(total_actual,total_target)) if total_target else None,{name for name,_ in eligible}
    avg,eligible=average(-1);previous,previous_eligible=average(-2)
    today_start=factory_day_start(factory_date_of(now))
    today=[line_intervals(data,l,today_start,now) for l in data['lines']]
    issues=[dict(id=f['id'],line=f['production_line'],machine=f['machine'],reason=f['reason'],opened_at=f['opened_at'],open_minutes=number(calc.minutes_between(f['opened_at'],now)),engineering_status=f['engineering_status'],engineer=f.get('engineer'),production_stopped=f['production_status']=='Ongoing' and f['resolved_at'] is None) for f in data['faults'] if f['production_line'] in data['lines'] and (f['production_status']=='Ongoing' or (f.get('engineer_called') and f['engineering_status'] in ('Not Started','In Progress','Ongoing')))]
    issues.sort(key=lambda f:(not f['production_stopped'],-f['open_minutes'],f['id']))
    categories=[dict(line=l,**line_intervals(data,l,now-timedelta(days=7),now)) for l in data['lines']]
    latest=calc.latest(*(r.get('created_at') for r in data['readings']))
    return dict(generated_at=now,timezone='Europe/London',shift=current_shift_window(now).to_api(),recording_note='Latest recorded HMI state; not continuous machine telemetry.',latest_submission_at=latest,weekly=weekly_production(data,week,now),period=window.to_api(),lines=lines,summary=dict(status_counts=dict(Counter(l['status'] for l in lines)),ongoing_faults=sum(1 for f in issues if f['production_stopped']),planned_minutes=sum(t['planned_minutes'] for t in today),unplanned_minutes=sum(t['unplanned_minutes'] for t in today),planned_events=sum(t['planned_events'] for t in today),unplanned_events=sum(t['unplanned_events'] for t in today),changeover_minutes=sum(t['changeover_minutes'] for t in today),average_percent=avg,eligible_lines=len(eligible),previous_percent=previous if previous_eligible==eligible else None,average_method='Expected-pack weighted achievement for the same last completed clock hour; excludes missing, unscheduled, idle and unaccounted periods. Actual / expected x 100; not classical A x P x Q OEE.',today_start=today_start),issues=issues[:3],breakdowns=categories,thresholds=configured_thresholds(),breakdown_method='Recorded production-stop intervals, clipped to scheduled time; planned overlaps excluded. Overlapping faults allocated oldest-first once, not added twice. Durations can differ from Engineering ticket age.')
