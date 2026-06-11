import { useState } from 'react'
import { api, setSession } from '../api'

interface TokenOut {
  token: string
  role: string
  employee_id: number | null
}

export default function Login() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [registering, setRegistering] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    try {
      const path = registering ? '/api/auth/register' : '/api/auth/login'
      const res = await api<TokenOut>(path, { method: 'POST', body: { email, password } })
      setSession(res.token, res.role, res.employee_id)
      window.location.href = '/schedule'
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    }
  }

  return (
    <div className="login-wrap">
      <form className="panel login-box" onSubmit={submit}>
        <h1>Yeems OPM</h1>
        <p className="subtitle">Scheduling &amp; plus/minus</p>
        <label>Email</label>
        <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" required />
        <label>Password</label>
        <input
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          type="password"
          required
          minLength={8}
        />
        {error && <div className="error">{error}</div>}
        <button type="submit">{registering ? 'Create first manager account' : 'Sign in'}</button>
        <p className="muted" style={{ marginTop: 10 }}>
          First time setting up?{' '}
          <a href="#" onClick={(e) => { e.preventDefault(); setRegistering(!registering) }}>
            {registering ? 'Back to sign in' : 'Create the first manager account'}
          </a>
        </p>
      </form>
    </div>
  )
}
