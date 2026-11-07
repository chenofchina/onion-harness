import { createContext, useContext, useEffect, useMemo, useState } from 'react'

export type ThemeMode = 'system' | 'light' | 'dark'

type ThemeState = {
  mode: ThemeMode
  resolved: 'light' | 'dark'
  setMode: (mode: ThemeMode) => void
}

const ThemeContext = createContext<ThemeState | null>(null)
const STORAGE_KEY = 'onion.theme'

function resolve(mode: ThemeMode): 'light' | 'dark' {
  if (mode !== 'system') return mode
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function ThemeProvider({ children, initial }: { children: React.ReactNode; initial?: ThemeMode }) {
  const [mode, setModeState] = useState<ThemeMode>(() => {
    if (initial) return initial
    try {
      return (localStorage.getItem(STORAGE_KEY) as ThemeMode) || 'system'
    } catch {
      return 'system'
    }
  })
  const [resolved, setResolved] = useState<'light' | 'dark'>(() => resolve(mode))

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const update = () => setResolved(resolve(mode))
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [mode])

  useEffect(() => {
    document.documentElement.dataset.theme = resolved
    document.documentElement.style.colorScheme = resolved
    try {
      localStorage.setItem(STORAGE_KEY, mode)
    } catch {
      /* ignore */
    }
  }, [mode, resolved])

  const value = useMemo<ThemeState>(
    () => ({ mode, resolved, setMode: setModeState }),
    [mode, resolved],
  )
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeState {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme must be used inside ThemeProvider')
  return context
}