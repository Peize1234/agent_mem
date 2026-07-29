from __future__ import annotations

_DEMO_LAB_CSS = """
<style>
.block-container {
    padding-top: 2.8rem;
    padding-bottom: 0.8rem;
}
.demo-lab-title {
    margin: 0;
    font-size: 2rem;
    line-height: 1.15;
}
.demo-lab-subtitle {
    color: rgba(49, 51, 63, 0.68);
    margin: 0.2rem 0 0;
}
.demo-sandbox-summary {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 0.55rem;
    min-width: 0;
    padding-top: 0.25rem;
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
.demo-pipeline {
    --pending: #7c8798;
    --running: #2563eb;
    --succeeded: #16803c;
    --failed: #cf2f3f;
    --skipped: #9aa3b1;
    color: #1f2937;
    padding: 0.25rem 0.2rem 0.1rem;
}
.demo-pipeline-summary {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    color: #5b6575;
    font-size: 0.83rem;
    margin-bottom: 0.7rem;
}
.demo-foreground {
    display: flex;
    align-items: stretch;
    justify-content: center;
    gap: 0;
    min-width: 730px;
}
.demo-node {
    border: 1px solid color-mix(in srgb, var(--node-color) 55%, transparent);
    border-top: 4px solid var(--node-color);
    border-radius: 0.55rem;
    background: color-mix(in srgb, var(--node-color) 7%, white);
    min-width: 122px;
    max-width: 170px;
    padding: 0.5rem 0.58rem;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
}
.demo-node.current {
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--node-color) 32%, transparent);
}
.demo-node.disabled {
    border-style: dashed;
    opacity: 0.72;
}
.demo-node.pending { --node-color: var(--pending); }
.demo-node.running { --node-color: var(--running); }
.demo-node.succeeded { --node-color: var(--succeeded); }
.demo-node.failed { --node-color: var(--failed); }
.demo-node.skipped { --node-color: var(--skipped); }
.demo-node-title {
    font-weight: 650;
    font-size: 0.91rem;
    white-space: nowrap;
}
.demo-node-status {
    color: var(--node-color);
    font-size: 0.78rem;
    font-weight: 600;
    margin-top: 0.13rem;
}
.demo-node-meta,
.demo-node-error {
    color: #687385;
    font-size: 0.72rem;
    line-height: 1.25;
    margin-top: 0.22rem;
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
    min-width: 28px;
    font-size: 1.2rem;
}
.demo-fork-stem,
.demo-merge-stem {
    width: 2px;
    height: 18px;
    background: #aab2bf;
    margin: 0 auto;
}
.demo-branches {
    display: grid;
    grid-template-columns: repeat(3, minmax(142px, 1fr));
    gap: 1rem;
    border-top: 2px solid #aab2bf;
    border-bottom: 2px solid #aab2bf;
    padding: 18px 0;
    margin: 0 8%;
    position: relative;
}
.demo-branch {
    display: flex;
    justify-content: center;
    position: relative;
}
.demo-branch::before,
.demo-branch::after {
    content: "";
    position: absolute;
    left: 50%;
    width: 2px;
    height: 18px;
    background: #aab2bf;
}
.demo-branch::before { top: -18px; }
.demo-branch::after { bottom: -18px; }
.demo-refresh {
    display: flex;
    justify-content: center;
}
@media (max-width: 1100px) {
    .demo-foreground {
        justify-content: flex-start;
        overflow-x: auto;
        padding-bottom: 0.35rem;
    }
    .demo-branches {
        margin: 0 2%;
    }
    .demo-sandbox-summary {
        justify-content: flex-start;
    }
}
</style>
"""


def inject(st) -> None:
    st.markdown(_DEMO_LAB_CSS, unsafe_allow_html=True)
