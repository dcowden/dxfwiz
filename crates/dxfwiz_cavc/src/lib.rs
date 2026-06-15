use cavalier_contours::polyline::{
    BooleanOp, BooleanResult, PlineCreation, PlineSource, PlineVertex, Polyline,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

type VertexTuple = (f64, f64, f64);

fn to_polyline(vertices: Vec<VertexTuple>, closed: bool) -> PyResult<Polyline<f64>> {
    if vertices.len() < 2 {
        return Err(PyValueError::new_err(
            "polyline requires at least two vertices",
        ));
    }
    Ok(Polyline::from_iter(
        vertices
            .into_iter()
            .map(|(x, y, bulge)| PlineVertex::new(x, y, bulge)),
        closed,
    ))
}

fn from_polyline(polyline: &Polyline<f64>) -> Vec<VertexTuple> {
    polyline
        .iter_vertexes()
        .map(|vertex| (vertex.x, vertex.y, vertex.bulge))
        .collect()
}

fn boolean_op(operation: &str) -> PyResult<BooleanOp> {
    match operation {
        "or" | "union" => Ok(BooleanOp::Or),
        "and" | "intersection" => Ok(BooleanOp::And),
        "not" | "difference" => Ok(BooleanOp::Not),
        "xor" => Ok(BooleanOp::Xor),
        _ => Err(PyValueError::new_err(format!(
            "unsupported boolean operation: {operation}"
        ))),
    }
}

fn boolean_result_polylines(result: BooleanResult<Polyline<f64>>) -> Vec<Vec<VertexTuple>> {
    result
        .pos_plines
        .iter()
        .chain(result.neg_plines.iter())
        .map(|result_polyline| from_polyline(&result_polyline.pline))
        .collect()
}

#[pyfunction]
fn offset_polyline(
    vertices: Vec<VertexTuple>,
    distance: f64,
    closed: Option<bool>,
) -> PyResult<Vec<Vec<VertexTuple>>> {
    let polyline = to_polyline(vertices, closed.unwrap_or(true))?;
    Ok(polyline
        .parallel_offset(distance)
        .iter()
        .map(from_polyline)
        .collect())
}

#[pyfunction]
fn polyline_area(vertices: Vec<VertexTuple>, closed: Option<bool>) -> PyResult<f64> {
    Ok(to_polyline(vertices, closed.unwrap_or(true))?.area())
}

#[pyfunction]
fn polyline_length(vertices: Vec<VertexTuple>, closed: Option<bool>) -> PyResult<f64> {
    Ok(to_polyline(vertices, closed.unwrap_or(true))?.path_length())
}

#[pyfunction]
fn polyline_orientation(vertices: Vec<VertexTuple>, closed: Option<bool>) -> PyResult<String> {
    Ok(format!(
        "{:?}",
        to_polyline(vertices, closed.unwrap_or(true))?.orientation()
    )
    .to_lowercase())
}

#[pyfunction]
fn boolean_polylines(
    subject_vertices: Vec<VertexTuple>,
    clip_vertices: Vec<VertexTuple>,
    operation: &str,
) -> PyResult<Vec<Vec<VertexTuple>>> {
    let subject = to_polyline(subject_vertices, true)?;
    let clip = to_polyline(clip_vertices, true)?;
    Ok(boolean_result_polylines(
        subject.boolean(&clip, boolean_op(operation)?),
    ))
}

#[pymodule]
fn dxfwiz_cavc(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(offset_polyline, m)?)?;
    m.add_function(wrap_pyfunction!(polyline_area, m)?)?;
    m.add_function(wrap_pyfunction!(polyline_length, m)?)?;
    m.add_function(wrap_pyfunction!(polyline_orientation, m)?)?;
    m.add_function(wrap_pyfunction!(boolean_polylines, m)?)?;
    Ok(())
}
