from copy import deepcopy
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from src.api import app
from src import live_dashboard as live, management_auth
from src.live_dashboard_api import SiteTarget

NOW=datetime(2026,10,9,10,20,tzinfo=timezone.utc)
HOUR=datetime(2026,10,9,9,tzinfo=timezone.utc)

def fixture():
    run=dict(id=1,production_line='Rovema',line_technician='Liam',shift='Days',customer='Aldi',product='White Basmati',format='1 kg x 10',pack_weight_kg=Decimal('1'),packs_per_case=10,cases_per_pallet=100,target_speed_ppm=Decimal('100'),standard_speed_ppm=Decimal('100'),status='Active',started_at=HOUR,finished_at=None,total_pallets_completed=Decimal('3'),pallets_remaining=Decimal('7'),starting_pallets_remaining=Decimal('10'))
    reading=dict(id=1,production_run_id=1,hour_start=HOUR,pallets_completed=Decimal('3'),period_started_at=HOUR,period_ended_at=HOUR+timedelta(hours=1),shift_window_start=HOUR.replace(hour=5),created_at=HOUR+timedelta(hours=1))
    return dict(lines=['Rovema','GIC','Guill'],runs=[run],readings=[reading],planned=[],faults=[],stoppages=[],operating_changes=[],speed_changes=[],targets=[dict(week_start=date(2026,10,5),scope='site',production_line=None,target_tonnes=Decimal('6'),notes='Night trial',set_by='Manager')])

def snapshot(d=None):return live.build_snapshot(d or fixture(),live.reporting_week(date(2026,10,5)),NOW)

def test_weekly_tonnes_partial_pallet_historical_weights_and_formats():
    d=fixture();d['readings'][0]['pallets_completed']=Decimal('1.25');d['runs'][0].update(pack_weight_kg=Decimal('.5'),packs_per_case=8,cases_per_pallet=100)
    r=snapshot(d);assert r['weekly']['actual_tonnes']==.5
    assert r['weekly']['progress_percent']==pytest.approx(100*.5/6)

def test_repeated_hour_version_is_not_double_counted_and_correction_wins():
    d=fixture();d['readings'].append({**d['readings'][0],'id':2,'pallets_completed':Decimal('4')})
    r=snapshot(d);assert r['weekly']['actual_tonnes']==4;assert r['lines'][0]['last_hour']['line']['actual_packs']==4000

@pytest.mark.parametrize('pallets,expected',[(0,0),('2.4',40),('3',50),('4.2',70),('6',100),('11',183.3)])
def test_achievement_zero_and_above_target_are_not_capped(pallets,expected):
    d=fixture();d['readings'][0]['pallets_completed']=Decimal(pallets)
    r=snapshot(d);assert r['lines'][0]['last_hour']['line']['output_vs_target_percent']==pytest.approx(expected,abs=.01)

def test_common_hour_average_does_not_use_older_line_readings():
    d=fixture();old={**d['runs'][0],'id':2,'production_line':'GIC','started_at':HOUR-timedelta(hours=1)};d['runs'].append(old)
    d['readings'].append({**d['readings'][0],'id':2,'production_run_id':2,'hour_start':HOUR-timedelta(hours=1),'period_started_at':HOUR-timedelta(hours=1),'period_ended_at':HOUR})
    r=snapshot(d);assert r['summary']['eligible_lines']==1;assert r['summary']['average_percent']==50
    assert r['lines'][1]['last_hour']['line']['status']=='no_reading'

def test_unknown_speed_has_no_percentage():
    d=fixture();d['runs'][0]['standard_speed_ppm']=None
    assert snapshot(d)['lines'][0]['last_hour']['line']['output_vs_target_percent'] is None

def test_partial_hour_uses_actual_duration():
    d=fixture();d['runs'][0]['started_at']=HOUR+timedelta(minutes=30);d['readings'][0]['period_started_at']=d['runs'][0]['started_at']
    r=snapshot(d)['lines'][0]['last_hour']['line'];assert r['target_packs']==3000;assert r['output_vs_target_percent']==100

def test_not_scheduled_is_neither_breakdown_nor_zero_oee():
    d=fixture();d['stoppages']=[dict(id=1,production_line='GIC',kind='not_scheduled',reason=None,started_at=HOUR-timedelta(hours=2),ended_at=None,reference_speed_ppm=100)]
    r=snapshot(d);assert r['lines'][1]['status']=='not_scheduled';assert r['lines'][1]['last_hour']['line']['output_vs_target_percent'] is None
    assert r['summary']['unplanned_minutes']==0;assert r['summary']['status_counts']['not_scheduled']==1

def fault(id=1,**kw):
    return dict(id=id,production_line='Rovema',machine='Bagger',reason='Film torn',opened_at=HOUR,resolved_at=None,production_status='Ongoing',engineering_status='In Progress',engineer_called=True,engineer='Alfie',**kw)

def test_overlapping_stops_count_once_and_planned_takes_precedence():
    d=fixture();d['faults']=[fault(1),fault(2)]
    d['planned']=[dict(id=1,production_run_id=1,reason='Film Change',started_at=HOUR,ended_at=HOUR+timedelta(minutes=20))]
    r=snapshot(d);assert r['summary']['planned_minutes']==20;assert r['summary']['unplanned_minutes']==60
    assert sum(c['minutes'] for c in r['breakdowns'][0]['breakdowns'])==60
    assert r['lines'][0]['last_hour']['line']['target_packs']==6000

def test_engineering_completion_does_not_restore_production():
    d=fixture();f=fault();f['engineering_status']='Resolved';d['faults']=[f]
    assert snapshot(d)['lines'][0]['status']=='stopped'

def test_production_restoration_does_not_close_engineering_and_age_is_not_downtime():
    d=fixture();f=fault();f.update(production_status='Resolved',resolved_at=HOUR+timedelta(minutes=10));d['faults']=[f]
    r=snapshot(d);assert r['summary']['ongoing_faults']==0;assert r['issues'][0]['open_minutes']==80
    assert r['summary']['unplanned_minutes']==10;assert not r['issues'][0]['production_stopped']

def test_resolved_fault_not_sent_to_engineering_is_not_an_ongoing_issue():
    d=fixture();f=fault();f.update(production_status='Resolved',resolved_at=HOUR+timedelta(minutes=10),engineer_called=False,engineering_status='Not Started');d['faults']=[f]
    assert snapshot(d)['issues']==[]

def test_no_run_activity_is_not_running_and_missing_data_is_not_zero():
    d=fixture();d['readings']=[];r=snapshot(d)
    assert r['lines'][0]['status']=='unknown';assert r['weekly']['actual_tonnes'] is None;assert r['lines'][0]['last_hour']['line']['actual_packs'] is None

def test_changeover_uses_full_stop_clock_not_acceptance_time():
    d=fixture();d['stoppages']=[dict(id=1,production_line='GIC',kind='changeover',reason=None,started_at=HOUR,ended_at=None,reference_speed_ppm=100,engineering_accepted_at=HOUR+timedelta(minutes=60),workflow={'kind':'format'})]
    r=snapshot(d);assert r['lines'][1]['changeover']['elapsed_minutes']==80;assert r['summary']['changeover_minutes']==80

def test_original_planned_quantity_and_reconciled_progress():
    d=fixture();d['runs'][0].update(total_pallets_completed=Decimal('12'),pallets_remaining=0)
    r=snapshot(d)['lines'][0]['job'];assert r['remaining_pallets']==0;assert r['progress_percent']==120

def test_fault_ranking_prefers_active_production_impact():
    d=fixture();restored=fault(1);restored.update(opened_at=HOUR-timedelta(hours=2),production_status='Resolved',resolved_at=HOUR)
    d['faults']=[restored,fault(2)];assert snapshot(d)['issues'][0]['id']==2

def test_week_boundary_and_midnight_shift():
    d=fixture();now=datetime(2026,10,10,0,30,tzinfo=timezone.utc)
    r=live.build_snapshot(d,live.reporting_week(date(2026,10,5)),now)
    assert 'Night' in r['shift']['label'];assert r['lines'][0]['trend'][-1]['hour_label'].startswith('00:00')
    assert live.reporting_start(datetime(2026,10,12,4,tzinfo=timezone.utc),d['targets'])==date(2026,10,5)

def test_custom_weekday_and_daylight_saving_week():
    targets=[dict(scope='site',week_start=date(2026,10,4))]
    assert live.reporting_start(NOW,targets)==date(2026,10,4)
    week=live.reporting_week(date(2026,10,19));assert (week.end-week.start).total_seconds()==169*3600

def test_live_and_targets_are_management_only():
    client=TestClient(app)
    assert client.get('/api/v1/dashboard/live').status_code==401
    assert client.post('/api/v1/management/live-weekly-target',json={}).status_code==401

def test_target_scope_rejects_unrecognised_site_or_extra_tenant():
    with pytest.raises(ValueError):SiteTarget(site='another-customer',week_start=date(2026,10,5),week_start_day=0,target_tonnes=120)
    with pytest.raises(ValueError):SiteTarget(week_start=date(2026,10,5),week_start_day=0,target_tonnes=120,tenant_id='other')


def test_zero_expected_output_is_not_divided():
    d=fixture();d['runs'][0]['standard_speed_ppm']=0
    assert snapshot(d)['lines'][0]['last_hour']['line']['output_vs_target_percent'] is None


def test_thresholds_are_configurable_and_invalid_settings_rejected(monkeypatch):
    monkeypatch.setenv('PULSE_DASHBOARD_AMBER_PERCENT','40')
    monkeypatch.setenv('PULSE_DASHBOARD_GREEN_PERCENT','70')
    assert snapshot()['thresholds']=={'amber':40,'green':70}
    monkeypatch.setenv('PULSE_DASHBOARD_GREEN_PERCENT','20')
    with pytest.raises(ValueError):snapshot()


def test_completed_run_still_reports_last_production_timestamp():
    d=fixture();d['runs'][0].update(status='Finished',finished_at=HOUR+timedelta(hours=1))
    r=snapshot(d)['lines'][0]
    assert r['job'] is None
    assert r['last_production_at']==HOUR+timedelta(hours=1)


def test_authenticated_snapshot_serializes_the_real_response_model(monkeypatch):
    prepared=snapshot()
    monkeypatch.setattr(live,'load_snapshot',lambda now,week_start:(fixture(),live.reporting_week(date(2026,10,5))))
    monkeypatch.setattr(live,'build_snapshot',lambda data,week,now:prepared)
    app.dependency_overrides[management_auth.require_management_session]=lambda:'Manager'
    try:
        response=TestClient(app).get('/api/v1/dashboard/live')
        assert response.status_code==200
        assert response.json()['lines'][0]['last_hour']['line']['actual_packs']==3000
        assert response.json()['weekly']['actual_tonnes']==3
    finally:app.dependency_overrides.pop(management_auth.require_management_session,None)


@pytest.mark.parametrize('moment,shift_name,completed', [
    ('2026-10-13T01:20:00+00:00', 'Night', 4),
    ('2026-10-13T05:00:00+00:00', 'Day', 0),
    ('2026-10-13T13:00:00+00:00', 'Afternoon', 0),
    ('2026-10-13T21:00:00+00:00', 'Night', 0),
    ('2026-10-25T05:30:00+00:00', 'Night', 8),
    ('2026-03-29T04:30:00+00:00', 'Night', 6),
])
def test_trend_only_contains_completed_hours_in_current_uk_shift(moment, shift_name, completed):
    now = datetime.fromisoformat(moment)
    result = live.build_snapshot(fixture(), live.reporting_week(now.date()), now)
    shift = live.current_shift_window(now)
    assert shift_name in result['shift']['label']
    for line in result['lines']:
        assert len(line['trend']) == completed
        assert all(shift.start <= h['hour_start'] < shift.end and h['hour_end'] <= now for h in line['trend'])
        assert line['last_hour']['hour_end'] == live.clock_hour_start(now)
