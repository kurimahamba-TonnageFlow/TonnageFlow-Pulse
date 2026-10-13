import {act,cleanup,fireEvent,render,screen,within,waitFor} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest'
import {LiveOperations,LiveOperationsView,Trend} from './LiveOperations'
import {WeeklyTargets} from '../management/WeeklyTargets'
import {getLive,saveLiveTarget} from './api'
import type {LiveSnapshot} from './liveTypes'
import fixtures from '../../../preview/live-fixtures.json'
const auth=vi.hoisted(()=>({session:{token:'manager-token'},handleAuthError:vi.fn(()=>false)}))
vi.mock('../management/session/useManagementSession',()=>({useManagementSession:()=>auth}))
vi.mock('./api',()=>({getLive:vi.fn(),saveLiveTarget:vi.fn()}))
const normal=()=>structuredClone(fixtures.normal) as unknown as LiveSnapshot
function view(data=normal(),error:string|null=null,now=new Date(data.generated_at).getTime()){
 return render(<MemoryRouter><LiveOperationsView data={data} error={error} now={now} loading={false} refresh={vi.fn()} week="" onWeek={vi.fn()}/></MemoryRouter>)
}
beforeEach(()=>{vi.clearAllMocks();vi.mocked(getLive).mockResolvedValue(normal());vi.mocked(saveLiveTarget).mockResolvedValue({status:'success'})})
afterEach(()=>{cleanup();vi.useRealTimers()})
describe('Live dashboard figures',()=>{
 it.each([[44.9,'danger'],[45,'warning'],[64.9,'warning'],[65,'success'],[100,'success'],[183.3,'success']])('keeps %s percent and its threshold colour',(value,color)=>{
  const d=normal();d.lines[0].last_hour!.line.output_vs_target_percent=value
  view(d);const gauge=screen.getByRole('img',{name:new RegExp(`Last-Hour OEE: ${value.toFixed(1)}`)})
  expect(gauge).toHaveClass(`lo-${color}`)
  expect(gauge.querySelector('.lo-ring')).toHaveAttribute('stroke-dasharray',`${Math.min(100,value)} 100`)
  if(value>100)expect(screen.getByText(/Over target/)).toBeInTheDocument()
 })
 it('shows missing and Not Scheduled without a fabricated zero',()=>{
  const d=normal();d.lines[0].last_hour!.line.output_vs_target_percent=null;d.lines[0].last_hour!.line.status='not_scheduled';view(d)
  expect(screen.getByRole('img',{name:/Last-Hour OEE: Not Scheduled/})).toBeInTheDocument()
  expect(screen.getByText(/actual packs.*expected packs.*100/)).toBeInTheDocument()
 })
 it('retains last figures and clearly warns on stale data',()=>{
  view(normal(),'Connection unavailable')
  expect(screen.getByText(/Stale snapshot/)).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('Showing the last successful snapshot')
  expect(screen.getByRole('article',{name:'Rovema performance'})).toBeInTheDocument()
 })
 it('opens breakdown records without leaving the dashboard',()=>{
  const d=normal();view(d);const category=d.breakdowns.find(l=>l.breakdowns.length)!
  fireEvent.click(screen.getByRole('button',{name:new RegExp(`${category.line}, ${category.breakdowns[0].category}:`)}))
  expect(screen.getByRole('region',{name:'Breakdown records'})).toHaveTextContent(category.breakdowns[0].records[0].reason)
  fireEvent.click(screen.getByRole('button',{name:'Close records'}));expect(screen.queryByRole('region',{name:'Breakdown records'})).not.toBeInTheDocument()
 })
 it('links into the existing filtered reports and target management',()=>{
  view();expect(within(screen.getByRole('article',{name:'Rovema performance'})).getByRole('link',{name:'Rovema'})).toHaveAttribute('href','/dashboard?view=reports&line=Rovema')
  expect(screen.getByRole('link',{name:/Manage target/})).toHaveAttribute('href','/management/weekly-targets?week=2026-10-05')
 })
})
describe('Live polling',()=>{
 it('waits until a request completes, refreshes at 30 seconds, and aborts on unmount',async()=>{
  vi.useFakeTimers();vi.setSystemTime(new Date(fixtures.normal.generated_at))
  let complete!:(d:LiveSnapshot)=>void
  vi.mocked(getLive).mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve}))
  const component=render(<MemoryRouter><LiveOperations/></MemoryRouter>)
  await act(async()=>{await vi.advanceTimersByTimeAsync(20000)})
  expect(getLive).toHaveBeenCalledTimes(1)
  await act(async()=>{complete(normal())})
  await act(async()=>{await vi.advanceTimersByTimeAsync(29999)})
  expect(getLive).toHaveBeenCalledTimes(1)
  await act(async()=>{await vi.advanceTimersByTimeAsync(1)})
  expect(getLive).toHaveBeenCalledTimes(2)
  const signal=vi.mocked(getLive).mock.calls[1][2]!
  component.unmount();expect(signal.aborted).toBe(true)
 })
 it('skips hidden polling and refreshes on return',async()=>{
  vi.useFakeTimers();const visibility=vi.spyOn(document,'visibilityState','get').mockReturnValue('hidden')
  render(<MemoryRouter><LiveOperations/></MemoryRouter>);await act(async()=>{})
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000)});expect(getLive).toHaveBeenCalledTimes(1)
  visibility.mockReturnValue('visible');await act(async()=>{document.dispatchEvent(new Event('visibilitychange'))})
  expect(getLive).toHaveBeenCalledTimes(2);visibility.mockRestore()
 })
})
describe('Weekly target management',()=>{
 it('saves the selected factory week, target and notes with the Management session',async()=>{
  render(<MemoryRouter initialEntries={['/management/weekly-targets?week=2026-10-05']}><WeeklyTargets/></MemoryRouter>)
  const input=await screen.findByLabelText('Target tonnes');fireEvent.change(input,{target:{value:'150.5'}})
  fireEvent.change(screen.getByLabelText('Target notes (optional)'),{target:{value:'Confirmed weekly plan'}})
  fireEvent.click(screen.getByRole('button',{name:'Save weekly target'}))
  await waitFor(()=>expect(saveLiveTarget).toHaveBeenCalledWith('manager-token',{site:'site',week_start:'2026-10-05',week_start_day:0,target_tonnes:150.5,notes:'Confirmed weekly plan'}))
  expect(await screen.findByRole('status')).toHaveTextContent('Weekly target saved')
 })
 it('keeps entered values when saving fails',async()=>{
  vi.mocked(saveLiveTarget).mockRejectedValue(new Error('This week overlaps an existing site target.'))
  render(<MemoryRouter><WeeklyTargets/></MemoryRouter>);await screen.findByLabelText('Target tonnes')
  fireEvent.click(screen.getByRole('button',{name:'Save weekly target'}))
  expect(await screen.findByRole('status')).toHaveTextContent('overlaps')
  expect(screen.getByLabelText('Target tonnes')).toHaveValue(120)
 })
})


describe('Current shift chart',()=>{
 it('shows the entire night and leaves unfinished hours blank',()=>{
  const shift={...normal().shift,label:'Night shift 2026-10-12',start:'2026-10-12T21:00:00Z',end:'2026-10-13T05:00:00Z'}
  const hour=structuredClone(normal().lines[0].last_hour!)
  hour.hour_start=shift.start;hour.line.output_vs_target_percent=0
  const {container,rerender}=render(<Trend hours={[hour]} shift={shift}/>)
  expect(screen.getByText(/Night shift.*22:00–06:00/)).toBeInTheDocument()
  expect(container.querySelectorAll('.lo-trend-labels span')).toHaveLength(8)
  expect(screen.getByText('22:00–23:00: 0.0%',{selector:'li'})).toBeInTheDocument()
  expect(screen.getByText('05:00–06:00: Awaiting completed hour')).toBeInTheDocument()
  expect(container.querySelectorAll('circle')).toHaveLength(1)
  rerender(<Trend hours={[]} shift={{...shift,label:'Day shift',start:shift.end,end:'2026-10-13T13:00:00Z'}}/>)
  expect(screen.getByText(/Day shift.*06:00–14:00/)).toBeInTheDocument()
  expect(container.querySelectorAll('circle')).toHaveLength(0)
 })
 it('retains both repeated UK hours when clocks go back',()=>{
  const {container}=render(<Trend hours={[]} shift={{...normal().shift,label:'Night shift',start:'2026-10-24T21:00:00Z',end:'2026-10-25T06:00:00Z'}}/>)
  expect(container.querySelectorAll('.lo-trend-labels span')).toHaveLength(9)
 })
})
