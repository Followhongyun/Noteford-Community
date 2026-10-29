import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles.css'
import './workspace-theme.css'
import { installAppleCorners } from './apple-corners/install.mjs'
import './apple-corners/apple-corners.css'

const platform = window.noteford?.platform
if (platform) document.documentElement.dataset.platform = platform

const disposeCorners = installAppleCorners(document.getElementById('root')!)
if (import.meta.hot) import.meta.hot.dispose(disposeCorners)

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
