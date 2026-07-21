import React from "react";

/** Phase 17 empty-state: never a blank panel when a bot has not pushed data. */
export default function PipelineGap({ bot }) {
  return (
    <div className="pipeline-gap" data-testid="pipeline-gap">
      pipeline gap for {bot}
    </div>
  );
}
