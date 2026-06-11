import { Navigate, NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { clearSession, getRole, getToken } from './api'
import Login from './pages/Login'
import SchedulePage from './pages/SchedulePage'
import EmployeesPage from './pages/EmployeesPage'
import RatingsPage from './pages/RatingsPage'
import ImportsPage from './pages/ImportsPage'
import SettingsPage from './pages/SettingsPage'
import MyAvailabilityPage from './pages/MyAvailabilityPage'

export default function App() {
  const navigate = useNavigate()
  const token = getToken()
  const role = getRole()

  if (!token) return <Login />

  const logout = () => {
    clearSession()
    navigate('/')
    window.location.reload()
  }

  return (
    <div className="app">
      <nav className="sidebar">
        <div className="brand">Yeems OPM</div>
        <NavLink to="/schedule">Schedule</NavLink>
        {role === 'manager' && <NavLink to="/employees">Employees</NavLink>}
        {role === 'manager' && <NavLink to="/ratings">Plus/Minus</NavLink>}
        {role === 'manager' && <NavLink to="/imports">Imports</NavLink>}
        {role === 'manager' && <NavLink to="/settings">Settings</NavLink>}
        {role === 'employee' && <NavLink to="/me">My availability</NavLink>}
        <div className="spacer" />
        <button className="secondary" onClick={logout} style={{ color: '#c9bfb4' }}>
          Sign out
        </button>
      </nav>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/schedule" replace />} />
          <Route path="/schedule" element={<SchedulePage />} />
          {role === 'manager' && <Route path="/employees" element={<EmployeesPage />} />}
          {role === 'manager' && <Route path="/ratings" element={<RatingsPage />} />}
          {role === 'manager' && <Route path="/imports" element={<ImportsPage />} />}
          {role === 'manager' && <Route path="/settings" element={<SettingsPage />} />}
          {role === 'employee' && <Route path="/me" element={<MyAvailabilityPage />} />}
          <Route path="*" element={<Navigate to="/schedule" replace />} />
        </Routes>
      </main>
    </div>
  )
}
