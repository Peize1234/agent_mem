from __future__ import annotations

from memory_monitor.components.common import filter_values, render_json


def render(st, service) -> None:
    st.header("Data browser")
    source = st.radio("Source", ["SQLite", "Vector store"], horizontal=True)
    if source == "SQLite":
        tables = service.list_tables()
        if not tables:
            st.warning("No supported SQLite tables were found.")
            return
        table = st.selectbox("Table", tables)
        schema = service.table_schema(table)
        columns = {item["name"] for item in schema}
        render_json(st, schema, label="Table schema")
        filter_columns = [name for name in ("user_id", "run_id", "trace_id", "job_id", "status") if name in columns]
        filters = {}
        input_columns = st.columns(max(len(filter_columns), 1))
        for index, name in enumerate(filter_columns):
            filters[name] = input_columns[index].text_input(name, key=f"table_{table}_{name}")
        page, page_size = st.columns(2)
        result = service.browse_table(
            table,
            page=int(page.number_input("Page", min_value=1, value=1, key=f"{table}_page")),
            page_size=int(page_size.selectbox("Page size", [20, 50, 100, 200], index=1)),
            filters=filter_values(**filters),
        )
        st.caption(f"{result['total']} rows")
        st.dataframe(result["items"], use_container_width=True, hide_index=True)
        render_json(st, result["items"], label="Formatted JSON")
        return

    collections = service.list_vector_collections()
    if not collections:
        st.warning("No Memory/vector-store instance is attached. Set MEMORY_MONITOR_MEMORY_CONFIG explicitly.")
        return
    collection = st.selectbox("Collection", [*collections, "midterm_tree"])
    user_id, run_id, trace_id = st.columns(3)
    filters = filter_values(
        user_id=user_id.text_input("user_id", key="vector_user"),
        run_id=run_id.text_input("run_id", key="vector_run"),
        trace_id=trace_id.text_input("trace_id", key="vector_trace"),
    )
    paging = st.columns(2)
    result = service.browse_vectors(
        collection,
        filters=filters,
        page=int(paging[0].number_input("Page", min_value=1, value=1, key="vector_page")),
        page_size=int(paging[1].selectbox("Page size", [20, 50, 100, 200], index=1, key="vector_page_size")),
    )
    render_json(st, result, label="Vector results", expanded=True)
