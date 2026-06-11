import { getEmployeeId } from '../api'
import { AvailabilityEditor, TimeOffEditor } from './EmployeesPage'

export default function MyAvailabilityPage() {
  const employeeId = getEmployeeId()
  if (employeeId == null) {
    return (
      <div>
        <h1>My availability</h1>
        <div className="panel muted">
          Your account isn't linked to an employee record yet — ask a manager to link it.
        </div>
      </div>
    )
  }
  return (
    <div>
      <h1>My availability</h1>
      <p className="subtitle">
        The scheduler only auto-assigns you inside these windows. No windows means you're treated
        as fully available.
      </p>
      <div className="panel">
        <AvailabilityEditor employeeId={employeeId} />
      </div>
      <div className="panel">
        <TimeOffEditor employeeId={employeeId} />
      </div>
    </div>
  )
}
