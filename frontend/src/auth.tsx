import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { fetchMe, login as apiLogin, logout as apiLogout } from './api'
import { AuthContext } from './auth-context'
import type { Member } from './types'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [member, setMember] = useState<Member | null>(null)
  const [ready, setReady] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const m = await fetchMe()
      setMember(m)
    } finally {
      setReady(true)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(async (username: string, password: string) => {
    const m = await apiLogin(username, password)
    setMember(m)
  }, [])

  const logout = useCallback(async () => {
    try {
      await apiLogout()
    } finally {
      setMember(null)
    }
  }, [])

  return (
    <AuthContext.Provider value={{ ready, member, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}
