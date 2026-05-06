import { createContext, useContext } from 'react'
import type { Member } from './types'

export interface AuthState {
  ready: boolean
  member: Member | null
  login(username: string, password: string): Promise<void>
  logout(): Promise<void>
  refresh(): Promise<void>
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider />')
  return ctx
}
