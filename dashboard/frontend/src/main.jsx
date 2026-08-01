import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import "./App.css";

class RootErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("Hermes dashboard crash", error, info);
  }

  render() {
    if (this.state.error) {
      const msg = this.state.error?.stack || String(this.state.error);
      return (
        <div className="dash-error" role="alert">
          <h1>Dashboard hit an error</h1>
          <p>Copy this and send it to Auto — then reload.</p>
          <pre>{msg}</pre>
          <button type="button" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </React.StrictMode>
);
