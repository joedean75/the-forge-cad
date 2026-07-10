import { Component, type ReactNode } from 'react'
import { Layout } from './components/Layout'

class ErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean; error?: Error }> {
  constructor(props: { children: ReactNode }) {
    super(props)
    this.state = { hasError: false }
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-cad-bg text-cad-text p-6 font-sans">
          <h1 className="font-display text-cad-danger text-lg font-semibold mb-2">DAPS failed to load</h1>
          <p className="text-sm mb-4">Something went wrong while rendering the application.</p>
          <pre className="bg-cad-panel p-3 rounded border border-cad-border text-xs overflow-auto">
            {this.state.error?.toString()}
          </pre>
          <p className="text-xs mt-4 text-cad-text-heading">
            Try clearing saved data or checking the browser console for details.
          </p>
        </div>
      )
    }
    return this.props.children
  }
}

function App() {
  return (
    <ErrorBoundary>
      <Layout />
    </ErrorBoundary>
  )
}

export default App
