import {render,screen} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {vi} from 'vitest'
import App from './App'
vi.stubGlobal('fetch',vi.fn(()=>new Promise(()=>{})))
test('renders the paper-trading safety state',()=>{render(<MemoryRouter><App/></MemoryRouter>);expect(screen.getByText('PAPER TRADING')).toBeInTheDocument()})
test('exposes the real research and validation navigation',()=>{render(<MemoryRouter initialEntries={['/validation']}><App/></MemoryRouter>);expect(screen.getByText('Validation evidence')).toBeInTheDocument();expect(screen.getByText('Observations')).toBeInTheDocument()})
test('exposes opportunity and reconciliation architecture screens',()=>{render(<MemoryRouter initialEntries={['/opportunities']}><App/></MemoryRouter>);expect(screen.getByText('Opportunity queue',{selector:'h1'})).toBeInTheDocument();expect(screen.getByText('Reconciliation')).toBeInTheDocument()})
test('settings makes live safety state explicit',()=>{render(<MemoryRouter initialEntries={['/settings']}><App/></MemoryRouter>);expect(screen.getByText('Live trading safeguard')).toBeInTheDocument();expect(screen.getByText('Live execution adapter is not installed.')).toBeInTheDocument()})
test('live paper route clearly separates real data from paper execution',async()=>{render(<MemoryRouter initialEntries={['/live-paper']}><App/></MemoryRouter>);expect(await screen.findByText('Binance live paper trading')).toBeInTheDocument();expect(screen.getByText('No real orders can be submitted.')).toBeInTheDocument()})
