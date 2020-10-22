#!/usr/bin/env python3
"""
Wrappers that permit duck-type array input to various functions.

Todo
----
Add wrappers for variance functions, spectral functions, and EOF function.

Note
-------
Cannot do xarray processing inside main functions because it must happen
outside of pint processing.
"""
import functools
import inspect
import itertools
import numbers
import re

import numpy as np

import pint
import pint.util as putil
import xarray as xr

from ..units import ureg
from . import warnings

# Regex to find terms surrounded by curly braces that can be filled with str.format()
REGEX_FORMAT = re.compile(r'\{([^{}]+?)\}')  # '+?' is non-greedy, group inside brackets


def _as_array(*args):
    """
    Convert list and tuple input to arrays.
    """
    return tuple(
        np.atleast_1d(_) if isinstance(_, (list, tuple, numbers.Number)) else _
        for _ in args
    )


def _get_default(func, param):
    """
    Get the default value from the call signature.
    """
    return inspect.signature(func).parameters[param].default


def _get_step(h):
    """
    Determine scalar step h.
    """
    h = np.atleast_1d(h)
    if h.size == 1:
        return h[0]
    elif h.ndim != 1:
        raise ValueError(f'x coordinates must be 1D, not {h.ndim}D.')
    elif not np.allclose(np.diff(h), h[1] - h[0]):
        raise ValueError(f'x coordinate steps must be identical, but got {h}.')
    else:
        return h[1] - h[0]


def _interp_safe(x, xp, yp):
    """
    Safe interpolation accounting for pint units. The `yp` can be a DataArray.
    """
    if any(isinstance(_, ureg.Quantity) for _ in (x, xp)):
        if not all(isinstance(_, ureg.Quantity) for _ in (x, xp)) or x.units != xp.units:  # noqa: E501
            raise ValueError('Source and destination coordinates must have same units.')
        xp = xp.magnitude
        x = x.magnitude
    units = None
    yp_in = yp
    if isinstance(yp, xr.DataArray):
        yp_in = yp.data
    if isinstance(yp_in, ureg.Quantity):
        units = yp_in.units
        yp_in = yp_in.magnitude
    if xp[1] - xp[0] < 0:
        xp, yp = xp[::-1], yp_in[::-1]
    y = np.interp(x, xp, yp_in)
    if units is not None:
        y = y * units
    if isinstance(yp, xr.DataArray):
        y = _dataarray_from(yp, y, keep_coords=False, keep_attrs=True)
    return y


def _pint_parse(units):
    """
    Parse `DataArray` unit attributes.
    """
    if isinstance(units, pint.Unit):
        return units
    units = re.sub(r'([a-zA-Z])([-+]?[0-9]+)', r'\1^\2', units or '')  # exponents↘
    if ' since ' in units:  # hours since, days since, etc.↘
        units = units.split()[0]
    num, *denom = units.split('/')
    return (
        ureg.parse_units(num)
        / np.prod((ureg.dimensionless, *map(ureg.parse_units, denom)))
    )


def _pint_remove(data):
    """
    Remove units before assigning as coordinate.
    """
    if isinstance(data, xr.DataArray) and isinstance(data.data, ureg.Quantity):
        data = data.copy()
        data.attrs.setdefault('units', format(data.data.units, '~'))
        data.data = data.data.magnitude
    return data


def _dataarray_data(data):
    """
    Return underlying `DataArray` data. Apply pint units if possible.
    """
    if isinstance(data.data, ureg.Quantity):
        data = data.data
    else:
        units = data.attrs.get('units', None)
        if units is None:
            data = data.data
        else:
            try:
                units = _pint_parse(units)
                data = data.data * units
            except Exception:  # many, many things could go wrong here
                warnings._warn_climopy(f'Failed to apply units {units!r} with pint.')
                data = data.data
    return data


def _dataarray_from(
    dataarray, data, name=None, dims=None, attrs=None, coords=None,
    dim_rename=None, dim_coords=None, keep_coords=True, keep_attrs=False,
):
    """
    Create a copy of the DataArray with various modifications.

    Parameters
    ----------
    dataarray : xarray.DataArray
        The source.
    data : array-like
        The new data.
    name, dims, attrs, coords : optional
        Replacement values.
    dim_rename : (dim1_old, dim1_new, dim2_old, dim2_new, ...), optional
        Mapping from old to new dimension names.
    dim_coords : (dim1, coords1, dim2, coords2, ...), optional
        Mapping to new array coordinates for arbitrary dimension(s).
    keep_coords : bool, optional
        Whether to keep the original coords.
    keep_attrs : bool, optional
        Whether to keep the original attributes.
    """
    # Get source info
    name = name or dataarray.name
    dims = dims or list(dataarray.dims)
    if coords is None:
        coords = dict(dataarray.coords)
    if attrs is None:
        attrs = dict(dataarray.attrs) if keep_attrs else {}

    # Rename dimension and optionally apply coordinates
    for dim_in, dim_out in (dim_rename or {}).items():
        coords.pop(dim_in, None)
        dims[dims.index(dim_in)] = dim_out
    for dim, coord in (dim_coords or {}).items():
        if coord is not None:
            coords[dim] = _pint_remove(coord)
        elif dim in coords:  # e.g. ('lat', None) is instruction do delete dimension
            del coords[dim]

    # Strip unit if present
    coords_fixed = {}
    for key, coord in coords.items():
        if not keep_coords:
            continue
        if not isinstance(coord, xr.DataArray):
            coord = xr.DataArray(coord, dims=key, name=key)
        if isinstance(coord.data, ureg.Quantity):
            coord.attrs.setdefault('units', format(coord.data.units, '~'))
            coord.data = coord.data.magnitude
        coords_fixed[key] = coord

    # Unquantify if DataArray was quantified
    if (
        not isinstance(dataarray.data, pint.Quantity)
        and isinstance(data.data, pint.Quantity)
    ):
        attrs['units'] = data.data.units
        data.data = data.data.magnitude

    # Return new dataarray
    return xr.DataArray(data, name=name, dims=dims, attrs=attrs, coords=coords_fixed)


def _dataarray_strip(func, x, *ys, suffix='', infer_axis=True, **kwargs):
    """
    Get data from *x* and *y* DataArrays, interpret the `dim` argument, and try to
    determine the `axis` used for computations automatically if ``infer_axis=True``.
    The axis suffix can be e.g. ``'_time'`` and we look for the ``dim_time`` keyword.
    """
    # Convert builtin python types to arraylike
    x, = _as_array(x)
    ys = _as_array(*ys)
    x_dataarray = isinstance(x, xr.DataArray)
    y_dataarray = all(isinstance(y, xr.DataArray) for y in ys)
    axis_default = _get_default(func, 'axis' + suffix)

    # Ensure all *ys* have identical dimensions, type, and shape
    y_types = {type(y) for y in ys}
    if len(y_types) != 1:
        raise ValueError(f'Expected one type for y inputs, got {y_types=}.')
    shapes = tuple(y.shape for y in ys)
    if any(_ != shapes[0] for _ in shapes):
        raise ValueError(f'Shapes should be identical, got {shapes}.')
    if y_dataarray:
        dims = tuple(y.dims for y in ys)
        if any(_ != dims[0] for _ in dims):
            raise ValueError(f'Dimensions should be identical, got {dims}.')

    # Interpret 'dim' argument when input is Dataarray
    axis = kwargs.pop('axis' + suffix, None)
    if y_dataarray:
        dim = kwargs.pop('dim' + suffix, None)
        if dim is not None:
            if axis is not None:
                warnings._warn_climopy('Ambiguous axis specification.')
            axis = ys[0].dims.index(dim)

    # If both are dataarrays and *x* is coordinates, infer axis from dimension names
    # Detect if user input axis or dim conflicts with inferred one
    # NOTE: Easier to do this than permit *omitting* x coordinates and retrieving
    # coordinates from DataArray. Variable call signatures get tricky.
    if infer_axis and x_dataarray and y_dataarray and x.ndim == 1 and x.dims[0] in ys[0].dims:  # noqa: E501
        axis_inferred = ys[0].dims.index(x.dims[0])
        if axis is not None and axis != axis_inferred:
            raise ValueError(f'Input {axis=} different from {axis_inferred=}.')
        axis = axis_inferred

    # Apply units and get data
    kwargs['axis' + suffix] = axis_default if axis is None else axis
    if x_dataarray:
        x = _dataarray_data(x)
    if y_dataarray:
        ys = (_dataarray_data(y) for y in ys)

    return (x, *ys, kwargs)


def _xarray_fit_wrapper(func):
    """
    Generic `xarray.DataArray` wrapper for functions accepting *x* and *y*
    and returning a fit parameter, the fit standard error, and the reconstruction.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input
        x, y = args  # *both* or *one* of these is dataarray
        x_in, y_in, kwargs = _dataarray_strip(func, x, y, **kwargs)

        # Call main function
        fit_val, fit_err, fit_line = func(x_in, y_in, **kwargs)

        # Create output array
        if isinstance(y, xr.DataArray):
            dim_coords = {y.dims[kwargs['axis']]: None}
            fit_val = _dataarray_from(y, fit_val, dim_coords=dim_coords)
            fit_err = _dataarray_from(y, fit_err, dim_coords=dim_coords)
            fit_line = _dataarray_from(y, fit_line)  # same everything

        return fit_val, fit_err, fit_line

    return wrapper


def _xarray_yy_wrapper(func):
    """
    Generic `xarray.DataArray` wrapper for functions accepting and returning
    arrays of the same shape. Permits situation where dimension coordinates
    of returned data are symmetrically trimmed.
    """
    @functools.wraps(func)
    def wrapper(y, *args, **kwargs):
        # Sanitize input
        _, y_in, kwargs = _dataarray_strip(func, 0, y, **kwargs)

        # Call main function
        y_out = func(y_in, *args, **kwargs)

        # Create output array
        if isinstance(y, xr.DataArray):
            axis = kwargs['axis']
            dim = y.dims[axis]
            ntrim = (y_in.shape[axis] - y_out.shape[axis]) // 2
            dim_coords = None
            if ntrim > 0 and dim in y.coords:
                dim_coords = {dim: y.coords[dim][ntrim:-ntrim]}
            y_out = _dataarray_from(y, y_out, dim_coords=dim_coords)

        return y_out

    return wrapper


def _xarray_xyy_wrapper(func):
    """
    Generic `xarray.DataArray` wrapper for functions accepting *x* and *y*
    coordinates and returning just *y* coordinates. Permits situation
    where dimension coordinates of returned data are symmetrically trimmed.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input
        x, y = args  # *both* or *one* of these is dataarray
        x_in, y_in, kwargs = _dataarray_strip(func, x, y, **kwargs)

        # Call main function
        y_out = func(x_in, y_in, **kwargs)

        # Create output array
        # NOTE: Account for symmetrically trimmed coords here
        if isinstance(y, xr.DataArray):
            axis = kwargs['axis']
            dim = y.dims[axis]
            dx = (y_in.shape[axis] - y_out.shape[axis]) // 2
            if dx > 0 and dim in y.coords:
                dim_coords = {dim: y.coords[y.dims[axis]][dx:-dx]}
            else:
                dim_coords = None
            y_out = _dataarray_from(y, y_out, dim_coords=dim_coords)

        return y_out

    return wrapper


def _xarray_xyxy_wrapper(func):
    """
    Generic `xarray.DataArray` wrapper for functions accepting *x* and *y*
    coordinates and returning new *x* and *y* coordinates.

    Warning
    -------
    So far this fails for 2D `xarray.DataArray` *x* data with non-empty
    coordinates for the `dim` dimension.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input
        x, y = args  # *both* or *one* of these is dataarray
        x_in, y_in, kwargs = _dataarray_strip(func, x, y, **kwargs)

        # Call main function
        x_out, y_out = func(x_in, y_in, **kwargs)

        # Create output array with x coordinates trimmed or interpolated to half-levels
        # NOTE: Also modify coordinates associated with x array, which may differ
        # from array values themselves (e.g. heights converted from pressure).
        axis = kwargs['axis']
        if isinstance(y, xr.DataArray):
            dim = y.dims[axis]
            if dim in y.coords:
                dim_coords = {dim: _interp_safe(x_out, x_in, y.coords[dim])}
            else:
                dim_coords = None
            y_out = _dataarray_from(y, y_out, dim_coords=dim_coords)
        if isinstance(x, xr.DataArray):
            dim = x.dims[0] if x.ndim == 1 else x.dims[axis]
            if dim in x.coords:
                dim_coords = {dim: _interp_safe(x_out, x_in, x.coords[dim])}
            else:
                dim_coords = None
            x_out = _dataarray_from(x, x_out, dim_coords=dim_coords)

        return x_out, y_out

    return wrapper


def _xarray_power_wrapper(func):
    """
    Support `xarray.DataArray` for `power` and `copower`.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input
        x, *ys = args  # *both* or *one* of these is dataarray
        x_in, *ys_in, kwargs = _dataarray_strip(func, x, *ys, **kwargs)

        # Call main function
        f, *Ps = func(x_in, *ys_in, **kwargs)

        # Create output array
        y = ys[0]
        if isinstance(x, xr.DataArray):
            f = _dataarray_from(
                x, f, dims=('f',), coords={}, attrs={'long_name': 'frequency'},
            )
        if isinstance(y, xr.DataArray):
            dim = y.dims[kwargs['axis']]
            Ps = (
                _dataarray_from(y, P, dim_rename={dim: 'f'}, dim_coords={'f': f})
                for P in Ps
            )

        return f, *Ps

    return wrapper


def _xarray_power2d_wrapper(func):
    """
    Support `xarray.DataArray` for `power2d` and `copower2d`.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input
        x1, x2, *ys = args  # *both* or *one* of these is dataarray
        x1_in, *ys_in, kwargs = _dataarray_strip(func, x1, *ys, suffix='_lon', **kwargs)
        x2_in, *_, kwargs = _dataarray_strip(func, x2, *ys, suffix='_time', **kwargs)

        # Call main function
        k, f, *Ps = func(x1_in, x2_in, *ys_in, **kwargs)

        # Create output array
        y = ys[0]
        if isinstance(x1, xr.DataArray):
            k = _dataarray_from(
                x1, dims=('k',), coords={}, attrs={'long_name': 'wavenumber'},
            )
        if isinstance(x2, xr.DataArray):
            f = _dataarray_from(
                x2, dims=('f',), coords={}, attrs={'long_name': 'frequency'},
            )
        if isinstance(y, xr.DataArray):
            dim1 = y.dims[kwargs['axis_lon']]
            dim2 = y.dims[kwargs['axis_time']]
            Ps = (
                _dataarray_from(
                    y, P, dim_rename={dim1: 'k', dim2: 'f'}, dim_coords={'k': k, 'f': f}
                )
                for P in Ps
            )

        return k, *Ps

    return wrapper


def _xarray_covar_wrapper(func):
    """
    Support `xarray.DataArray` for `corr`, `covar`, `autocorr`, and `autocovar` funcs.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input
        x, *ys = args  # *both* or *one* of these is dataarray
        x_in, *ys_in, kwargs = _dataarray_strip(func, x, *ys, **kwargs)

        # Call main function
        lag, C = func(x_in, *ys_in, **kwargs)

        # Create output array
        y = ys[0]
        if isinstance(x, xr.DataArray):
            lag = _dataarray_from(x, lag, name='lag', dims=('lag',), coords={})
        if isinstance(y, xr.DataArray):
            dim = y.dims[kwargs['axis']]
            C = _dataarray_from(y, C, dim_rename={dim: 'lag'}, dim_coords={'lag': lag})

        return lag, C

    return wrapper


def _xarray_eof_wrapper(func):
    """
    Support `xarray.DataArray` for `eof` functions.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input and interpret 'dim' arguments
        data, = data_in, = args
        axis_time = kwargs.pop('axis_time', _get_default(func, 'axis_time'))
        axis_space = kwargs.pop('axis_space', _get_default(func, 'axis_space'))
        if isinstance(data, xr.DataArray):
            data_in = data.data
            if 'dim_time' in kwargs:  # no warning for duplicate args, no big deal
                axis_time = data.dims.index(kwargs.pop('dim_time'))
            if 'dim_space' in kwargs:
                axis_space = data.dims.index(kwargs.pop('dim_space'))

        # Call main function
        pcs, projs, evals, nstars = func(
            data_in, axis_time=axis_time, axis_space=axis_space, **kwargs,
        )

        # Create output arrays
        if isinstance(data, xr.DataArray):
            # Add EOF dimension
            dims = ['eof'] + list(data.dims)  # add 'EOF number' leading dimension
            eofs = np.arange(1, pcs.shape[0] + 1)

            # Remove dimension coordinates where necessary
            time_flat = {data.dims[axis_time]: None}
            space_flat = {data.dims[axis]: None for axis in np.atleast_1d(axis_space)}
            both_flat = {**time_flat, **space_flat}
            all_flat = both_flat.copy()  # with singleton EOF dimension
            both_flat['eof'] = time_flat['eof'] = space_flat['eof'] = eofs

            # Create DataArrays
            pcs = _dataarray_from(data, pcs, dims=dims, dim_coords=space_flat)
            projs = _dataarray_from(data, projs, dims=dims, dim_coords=time_flat)
            evals = _dataarray_from(data, evals, dims=dims, dim_coords=both_flat)
            nstars = _dataarray_from(data, nstars, dims=dims, dim_coords=all_flat)

        return pcs, projs, evals, nstars

    return wrapper


def _xarray_hist_wrapper(func):
    """
    Support `xarray.DataArray` for `hist` function.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input
        # NOTE: This time 'x' coordinates are bins so do not infer
        yb, y = args
        yb_in, y_in, kwargs = _dataarray_strip(func, yb, y, infer_axis=False, **kwargs)

        # Call main function
        y_out = func(yb_in, y_in, **kwargs)

        # Add metadata to y_out
        if isinstance(y, xr.DataArray):
            dim = y.dims[kwargs['axis']]
            name = y.name
            yb_centers = 0.5 * (yb_in[1:] + yb_in[:-1])
            coords = _dataarray_from(
                y, yb_centers, dims=(name,), coords={}, keep_attrs=True,
            )
            y_out = _dataarray_from(
                y, y_out, name='count', attrs={},
                dim_rename={dim: name}, dim_coords={name: coords},
            )

        return y_out

    return wrapper


def _xarray_zerofind_wrapper(func):
    """
    Support `xarray.DataArray` for zerofind function.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Sanitize input
        x, y = args
        x_in, y_in, kwargs = _dataarray_strip(func, x, y, **kwargs)

        # Call main function
        x_out, y_out = func(x_in, y_in, **kwargs)

        # Add metadata to x_out and y_out
        # NOTE: x_out inherits *shape* from y_out but should inherit *attrs* from x_in.
        if isinstance(y, xr.DataArray):
            attrs = {}
            dim = y.dims[kwargs['axis']]
            if dim in y.coords:
                attrs = y.coords[dim].attrs
            x_out = _dataarray_from(
                y, x_out, name=dim, attrs=attrs, dim_rename={dim: 'track'},
            )
            y_out = _dataarray_from(
                y, y_out, keep_attrs=True, dim_rename={dim: 'track'},
            )

        return x_out, y_out

    return wrapper


def _pint_wrapper(units_in, units_out, strict=False, **fmt_defaults):
    """
    Handle pint units, similar to `~pint.UnitRegistry.wraps`. Put input units
    as the first argument, set `strict` to ``False`` by default, and if
    non-quantities are passed into the function, ensure non-quantities are returned
    by the function rather than a quantity with ``'dimensionless'`` units, and
    default to ``strict=False``.

    Parameters
    ----------
    units_in : unit-like, string, or list thereof
        A pint unit like `~pint.UnitRegistry.meter`, a relational string
        like ``'=x ** 2'``, or list thereof, specifying the units of the
        input data. You can put keyword arguments into the unit specification
        with, for example ``_pint_wrapper(('=x', '=y'), '=y / x ** {{n}}')``
        for the ``n``th derivative.
    units_out : unit-like, string, or list thereof
        As with `units_in` but for the output arguments.
    strict : bool, optional
        Whether non-relational (absolute) unit specifications are strict.
        If ``False``, non-quantity input is cast to the default units.
    **fmt_defaults
        Default values for the terms surrounded by curly braces in relational
        or string unit specifications.

    Example
    -------
    Here is a simple example for a derivative wrapper.

    >>> import climopy as climo
    ... from climopy.internals import quack
    ... ureg = climo.ureg
    ... @quack._pint_wrapper(('=x', '=y'), '=y / x ** {order}', order=1)
    ... def deriv(x, y, order=1):
    ...     return y / x ** order
    ... deriv(1 * ureg.m, 1 * ureg.s, order=2)
    1.0 <Unit('second / meter ** 2')>

    """
    # Handle singleton input or multiple input
    is_container_in = isinstance(units_in, (tuple, list))
    is_container_out = isinstance(units_out, (list, tuple))
    if not is_container_in:
        units_in = (units_in,)
    if not is_container_out:
        units_out = (units_out,)

    # Ensure valid kwargs
    # NOTE: Pint cannot handle singleton-tuple of return value unit specifications.
    # So when passing to wrapper simply expand singleton tuples.
    fmt_args = []
    for units in (units_in, units_out):
        for unit in units:
            if isinstance(unit, str):
                fmt_args.extend(REGEX_FORMAT.findall(unit))
    if set(fmt_args) != set(fmt_defaults):
        raise ValueError(
            f'Invalid or insufficient keyword args {tuple(fmt_defaults)} '
            f'when string unit specification includes terms {tuple(fmt_args)}.'
        )

    # Ensure valid args unit specifications
    for arg in units_in:
        if arg is not None and not isinstance(arg, (ureg.Unit, str)):
            raise TypeError(
                f'Wraps arguments must by of type str or Unit, not {type(arg)} ({arg}).'
            )

    # Ensure valid return unit specifications
    for arg in units_out:
        if arg is not None and not isinstance(arg, (ureg.Unit, str)):
            raise TypeError(
                "Wraps 'units_out' argument must by of type str or Unit, "
                f'not {type(arg)} ({arg}).'
            )

    # Parse input
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Test input
            n_expect = len(units_in)
            n_result = len(args)
            if len(units_in) != len(args):
                raise ValueError(f'Expected {n_expect} positional args, got {n_result}.')  # noqa: E501

            # Fill parameters inside units
            units_in_fmt = []
            units_out_fmt = []
            for units, units_fmt in zip(
                (units_in, units_out), (units_in_fmt, units_out_fmt)
            ):
                for unit in units:
                    if isinstance(unit, str):
                        # Get format values from user input keyword args
                        fmt_keys = REGEX_FORMAT.findall(unit)
                        fmt_kwargs = {
                            key: value for key, value in kwargs.items()
                            if key in fmt_keys
                        }
                        # Fill missing format values and format string
                        for key, value in fmt_defaults.items():
                            fmt_kwargs.setdefault(key, value)
                        unit = unit.format(**fmt_kwargs)
                    # Add new unit string
                    units_fmt.append(unit)

            # Dequantify input
            converter = _parse_pint_args(units_in_fmt)
            args_new, args_by_name = converter(args, strict)

            # Call main function and check output
            result = func(*args_new, **kwargs)
            n_result = 1 if not isinstance(result, tuple) else len(result)
            n_expect = len(units_out)
            if not is_container_out and isinstance(result, tuple):
                raise ValueError('Got tuple of return values, expected one value.')
            if is_container_out and n_result != len(units_out):
                raise ValueError(f'Expected {n_expect} return values, got {n_result}.')

            # Quantify output, but *only* if input was quantities
            if not is_container_out:
                result = (result,)
            pairs = tuple(_to_units_container(arg) for arg in units_out_fmt)
            no_quantities = not any(isinstance(arg, ureg.Quantity) for arg in args)
            units = tuple(
                _replace_units(unit, args_by_name) if is_ref else unit
                for (unit, is_ref) in pairs
            )
            result = tuple(
                res if unit is None or no_quantities else ureg.Quantity(res, unit)
                for unit, res in itertools.zip_longest(units, result)
            )

            # Return sanitized values
            if not is_container_out:
                result = result[0]
            return result

        return wrapper

    return decorator


def _parse_pint_args(args):
    """
    Parse pint wrapper arguments.
    """
    # Helper variables
    defs_args = set()  # arguments which contain definitions
    defs_args_ndx = set()  # (i.e. names that appear alone and for the first time)
    dependent_args_ndx = set()  # arguments which depend on others
    unit_args_ndx = set()  # arguments which have units.
    args_as_uc = [_to_units_container(arg) for arg in args]

    # Check for references in args, remove None values
    for ndx, (arg, is_ref) in enumerate(args_as_uc):
        if arg is None:
            continue
        elif is_ref:
            if len(arg) == 1:
                [(key, value)] = arg.items()
                if value == 1 and key not in defs_args:
                    # This is the first time that
                    # a variable is used => it is a definition.
                    defs_args.add(key)
                    defs_args_ndx.add(ndx)
                    args_as_uc[ndx] = (key, True)
                else:
                    # The variable was already found elsewhere,
                    # we consider it a dependent variable.
                    dependent_args_ndx.add(ndx)
            else:
                dependent_args_ndx.add(ndx)
        else:
            unit_args_ndx.add(ndx)

    # Check that all valid dependent variables
    for ndx in dependent_args_ndx:
        arg, is_ref = args_as_uc[ndx]
        if not isinstance(arg, dict):
            continue
        if not set(arg.keys()) <= defs_args:
            raise ValueError(
                'Found a missing token while wrapping a function: '
                f'Not all variable referenced in {args[ndx]} are defined!'
            )

    # Generate converter
    def _converter(args, strict):
        args_new = list(args)
        args_by_name = {}

        # First pass: Grab named values
        for ndx in defs_args_ndx:
            arg = args[ndx]
            args_by_name[args_as_uc[ndx][0]] = arg
            args_new[ndx] = getattr(arg, '_magnitude', arg)

        # Second pass: calculate derived values based on named values
        for ndx in dependent_args_ndx:
            arg = args[ndx]
            assert _replace_units(args_as_uc[ndx][0], args_by_name) is not None
            args_new[ndx] = ureg._convert(
                getattr(arg, '_magnitude', arg),
                getattr(arg, '_units', putil.UnitsContainer({})),
                _replace_units(args_as_uc[ndx][0], args_by_name),
            )

        # Third pass: convert other arguments
        for ndx in unit_args_ndx:
            if isinstance(args[ndx], ureg.Quantity):
                args_new[ndx] = ureg._convert(
                    args[ndx]._magnitude, args[ndx]._units, args_as_uc[ndx][0]
                )
            elif strict:
                if isinstance(args[ndx], str):
                    # If the value is a string, we try to parse it
                    tmp_value = ureg.parse_expression(args[ndx])
                    args_new[ndx] = ureg._convert(
                        tmp_value._magnitude, tmp_value._units, args_as_uc[ndx][0]
                    )
                else:
                    raise ValueError(
                        'A wrapped function using strict=True requires '
                        'quantity or a string for all arguments with not None units. '
                        f'(error found for {args_as_uc[ndx][0]}, {args_new[ndx]})'
                    )

        return args_new, args_by_name

    return _converter


def _replace_units(original_units, values_by_name):
    """
    Convert a unit compatible type to a UnitsContainer.
    """
    q = 1
    with np.errstate(divide='ignore', invalid='ignore'):
        # NOTE: Multiply quantities successively here just to pull out units
        # after everything is done. But avoid broadcasting errors as shown.
        for arg_name, exponent in original_units.items():
            try:
                q = q * values_by_name[arg_name] ** exponent
            except ValueError:  # avoid negative integer powers of integer arrays
                q = q * values_by_name[arg_name].astype(float) ** exponent
            m = getattr(q, 'magnitude', q)
            u = getattr(q, 'units', 1)
            if not np.isscalar(m):
                m = m.flat[0]
            q = m * u
    return getattr(q, '_units', putil.UnitsContainer({}))


def _to_units_container(arg):
    """
    Convert a unit compatible type to a UnitsContainer, checking if it is string
    field prefixed with an equal (which is considered a reference). Return the
    unit container and a boolean indicating whether this is an equal string.
    """
    if isinstance(arg, str) and '=' in arg:
        return putil.to_units_container(arg.split('=', 1)[1]), True
    return putil.to_units_container(arg, ureg), False
