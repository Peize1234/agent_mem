from __future__ import annotations

_DEMO_LAB_CSS = """
<style>
.block-container {
    padding-top: 2.45rem;
    padding-bottom: 0.65rem;
}
.demo-lab-title {
    margin: 0;
    font-size: 2rem;
    line-height: 1.12;
}
.demo-lab-subtitle {
    color: rgba(49, 51, 63, 0.68);
    margin: 0.14rem 0 0;
}
.demo-sandbox-summary {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 0.55rem;
    min-width: 0;
    padding-top: 0.15rem;
}
.demo-sandbox-id {
    font-weight: 600;
    white-space: nowrap;
}
.demo-sandbox-path {
    color: rgba(49, 51, 63, 0.65);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 30rem;
}
div[class*="st-key-chat_history_"] {
    overscroll-behavior-y: contain;
    scrollbar-gutter: stable;
}
div[class*="st-key-right_workspace_"] {
    min-height: 690px;
}
div[class*="st-key-active_turn_"] button {
    border: 1px solid rgba(124, 135, 152, 0.34);
    border-left-width: 4px;
    border-radius: 0.5rem;
    min-height: 3.65rem;
    justify-content: flex-start;
    text-align: left;
}
div[class*="st-key-active_turn_"] button p {
    line-height: 1.25;
    text-align: left;
}

.demo-pipeline {
    --pending: #7c8798;
    --queued: #c47a10;
    --running: #2563eb;
    --succeeded: #16803c;
    --failed: #cf2f3f;
    --skipped: #9aa3b1;
    color: #1f2937;
    container-type: inline-size;
    padding: 0.12rem 0.08rem 0.05rem;
}
.demo-pipeline-summary {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    color: #5b6575;
    font-size: 0.74rem;
    margin-bottom: 0.3rem;
}
.demo-pipeline-scroll {
    overflow-x: hidden;
    overscroll-behavior-x: contain;
    min-height: 370px;
    padding: 0.05rem 0.08rem 0.18rem;
}
.demo-flow-row {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-wrap: nowrap;
    gap: 0;
    width: 100%;
    min-width: 0;
    min-height: 350px;
}
.demo-foreground-chain {
    display: flex;
    align-items: center;
    flex: 0 1 auto;
    min-width: 0;
}
.demo-node {
    border: 1px solid color-mix(in srgb, var(--node-color) 55%, transparent);
    border-top: 3px solid var(--node-color);
    border-radius: 0.45rem;
    background: color-mix(in srgb, var(--node-color) 7%, white);
    box-sizing: border-box;
    min-width: 0;
    padding: 0.3rem 0.38rem;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
    position: relative;
}
.demo-foreground-chain .demo-node {
    flex: 0 1 100px;
    width: clamp(92px, 7vw, 108px);
    max-width: 108px;
}
.demo-node.current {
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--node-color) 32%, transparent);
}
.demo-node.disabled {
    border-style: dashed;
    opacity: 0.74;
}
.demo-node.held {
    border-style: dashed;
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--pending) 20%, transparent);
}
.demo-node.pending { --node-color: var(--pending); }
.demo-node.queued { --node-color: var(--queued); }
.demo-node.running { --node-color: var(--running); animation: demo-node-pulse 1.5s ease-in-out infinite; }
.demo-node.succeeded { --node-color: var(--succeeded); }
.demo-node.failed { --node-color: var(--failed); }
.demo-node.skipped { --node-color: var(--skipped); }
.demo-node-title {
    font-weight: 650;
    font-size: 0.79rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.demo-node-status {
    color: var(--node-color);
    font-size: 0.69rem;
    font-weight: 600;
    margin-top: 0.07rem;
    white-space: nowrap;
}
.demo-node-meta,
.demo-node-detail,
.demo-node-error {
    color: #687385;
    font-size: 0.64rem;
    line-height: 1.18;
    margin-top: 0.1rem;
}
.demo-node-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 0.18rem;
    margin-top: 0.15rem;
}
.demo-node-badge {
    background: color-mix(in srgb, var(--node-color) 12%, white);
    border: 1px solid color-mix(in srgb, var(--node-color) 28%, transparent);
    border-radius: 999px;
    color: #4b5563;
    font-size: 0.59rem;
    line-height: 1.1;
    padding: 0.08rem 0.24rem;
    white-space: nowrap;
}
.demo-node-popover {
    --demo-popover-body-font-size: 0.9rem;
    --demo-popover-body-line-height: 1.55;
    --demo-popover-heading-font-size: 0.8rem;
    --demo-popover-label-font-size: 0.76rem;
    --demo-popover-meta-font-size: 0.74rem;
    --demo-popover-section-font-size: 0.84rem;
    --demo-popover-title-font-size: 0.875rem;
    background: #111827;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 0.65rem;
    box-shadow: 0 16px 42px rgba(15, 23, 42, 0.34);
    color: #e5e7eb;
    display: none;
    max-height: min(72vh, 720px);
    overscroll-behavior: contain;
    overflow: auto;
    padding: 0.85rem 0.9rem;
    position: fixed;
    right: 1.25rem;
    top: 4.75rem;
    width: min(680px, calc(100vw - 2.5rem));
    z-index: 100000;
}
.demo-node:hover .demo-node-popover,
.demo-node:focus-within .demo-node-popover {
    display: block;
}
.demo-popover-title {
    font-size: var(--demo-popover-title-font-size);
    font-weight: 700;
    line-height: 1.35;
    margin-bottom: 0.4rem;
}
.demo-popover-call {
    padding: 0.48rem 0 0.38rem;
}
.demo-popover-call + .demo-popover-call {
    border-top: 2px solid rgba(255, 255, 255, 0.2);
    margin-top: 0.58rem;
    padding-top: 0.72rem;
}
.demo-popover-call-title {
    color: #f9fafb;
    font-size: var(--demo-popover-section-font-size);
    font-weight: 650;
    line-height: 1.35;
}
.demo-popover-call-meta {
    color: #9ca3af;
    font-size: var(--demo-popover-meta-font-size);
    line-height: 1.4;
    margin-top: 0.08rem;
}
.demo-call-block {
    margin-top: 0.52rem;
}
.demo-call-heading {
    color: #f9fafb;
    font-size: var(--demo-popover-heading-font-size);
    font-weight: 700;
    line-height: 1.35;
    margin-bottom: 0.25rem;
}
.demo-prompt-message,
.demo-call-answer .demo-markdown-text {
    background: #030712;
    border-radius: 0.35rem;
    padding: 0.62rem 0.68rem;
}
.demo-prompt-message + .demo-prompt-message {
    margin-top: 0.42rem;
}
.demo-prompt-role {
    color: #93c5fd;
    font-size: var(--demo-popover-label-font-size);
    font-weight: 700;
    line-height: 1.35;
    margin-bottom: 0.25rem;
}
.demo-markdown-text {
    color: #d1d5db;
    font-family: inherit;
    font-size: var(--demo-popover-body-font-size);
    line-height: var(--demo-popover-body-line-height);
    overflow-wrap: anywhere;
    tab-size: 4;
    white-space: pre-wrap;
    word-break: break-word;
}
.demo-markdown-text :where(p, ul, ol, li, pre, code) {
    font-size: inherit;
    line-height: inherit;
}
.demo-markdown-text :where(p, ul, ol, pre) {
    margin: 0.35rem 0;
}
.demo-markdown-text :where(ul, ol) {
    padding-left: 1.25rem;
}
.demo-node-error {
    color: var(--failed);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.demo-arrow {
    color: #9aa3b1;
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 0 1 18px;
    min-width: 12px;
    max-width: 20px;
    font-size: 0.92rem;
}
.demo-parallel-arrow {
    align-self: center;
    display: flex;
    flex: 0 0 20px;
    width: 20px;
    min-width: 20px;
    height: 20px;
}
.demo-parallel-arrow-svg {
    display: block;
    width: 100%;
    height: 100%;
    overflow: visible;
}
.demo-parallel-arrow-svg line {
    stroke: #aab2bf;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
}
.demo-fork,
.demo-merge {
    align-self: stretch;
    flex: 0 0 24px;
    width: 24px;
    min-width: 0;
}
.demo-connector-svg {
    display: block;
    width: 100%;
    height: 100%;
    overflow: visible;
}
.demo-connector-svg line {
    stroke: #aab2bf;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
}
.demo-connector-arrowhead {
    fill: #aab2bf;
}
.demo-memory-column {
    display: flex;
    align-self: stretch;
    flex: 0 1 118px;
    flex-direction: column;
    justify-content: space-between;
    width: clamp(108px, 8vw, 122px);
    min-width: 0;
}
.demo-memory-branch {
    display: flex;
    align-items: center;
    flex: 0 0 25%;
    min-height: 0;
    padding-block: 0.3rem;
    box-sizing: border-box;
}
.demo-memory-branch .demo-node {
    width: 100%;
}
.demo-complete-node {
    display: flex;
    align-items: center;
    flex: 0 1 102px;
    width: clamp(94px, 7vw, 106px);
    min-width: 0;
}
.demo-complete-node .demo-node {
    width: 100%;
}
@keyframes demo-node-pulse {
    0%, 100% { box-shadow: 0 0 0 1px color-mix(in srgb, var(--node-color) 18%, transparent); }
    50% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--node-color) 28%, transparent); }
}
@media (max-width: 800px) {
    .demo-sandbox-summary {
        justify-content: flex-start;
    }
}
@container (max-width: 700px) {
    .demo-pipeline-scroll {
        overflow-x: auto;
        scrollbar-gutter: stable;
    }
    .demo-flow-row {
        width: 740px;
        min-width: 740px;
    }
}
</style>
"""


def inject(st) -> None:
    st.markdown(_DEMO_LAB_CSS, unsafe_allow_html=True)
