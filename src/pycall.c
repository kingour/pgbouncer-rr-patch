/*
 * pycall.c — Compatible with Python 2.7 and Python 3.x
 * For pgbouncer-rr extension (query rewriting / routing)
 */

#include <Python.h>
#include "bouncer.h"
#include <usual/pgutil.h>

char *pycall(PgSocket *client, char *username, char *query_str, int in_transaction,
             char *py_file, char *py_function, bool *failed) {

    PyObject *pName = NULL, *pModule = NULL, *pFunc = NULL;
    PyObject *pArgs = NULL, *pValue = NULL;
    PyObject *ptype, *perror, *ptraceback;
    char *res = NULL, *ext = NULL, *py_file_copy = NULL, *py_module = NULL, *py_path_env = NULL;

    *failed = true;

    /* --- Setup python path --- */
    py_file_copy = strdup(py_file);
    if (!py_file_copy) {
        slog_error(client, "Out of memory");
        return NULL;
    }

    char *dir = dirname(py_file_copy);
    py_path_env = malloc(strlen(dir) + 20);
    if (!py_path_env) {
        slog_error(client, "Out of memory");
        free(py_file_copy);
        return NULL;
    }

    setenv("PYTHONPATH", dir, 1);
    free(py_path_env);
    py_path_env = NULL;

    /* --- Derive module name --- */
    char *file_tmp = strdup(py_file);
    if (!file_tmp) {
        free(py_file_copy);
        return NULL;
    }
    py_module = basename(file_tmp);
    ext = strrchr(py_module, '.');
    if (ext) *ext = '\0';

    /* --- Initialize Python --- */
    Py_Initialize();

    /* --- Load module --- */
#if PY_MAJOR_VERSION >= 3
    pName = PyUnicode_FromString(py_module);
#else
    pName = PyString_FromString(py_module);
#endif
    if (!pName) {
        slog_error(client, "Python module <%s> did not load", py_module);
        goto finish;
    }

    pModule = PyImport_Import(pName);
    if (!pModule) {
        slog_error(client, "Python module <%s> did not load", py_module);
        goto finish;
    }

    /* --- Get target function --- */
    pFunc = PyObject_GetAttrString(pModule, py_function);
    if (!pFunc || !PyCallable_Check(pFunc)) {
        slog_error(client, "Python Function <%s> not found or not callable in <%s>", py_function, py_module);
        goto finish;
    }

    /* --- Prepare args: username, query_str, in_transaction --- */
    pArgs = PyTuple_New(3);
    if (!pArgs) {
        slog_error(client, "Out of memory creating args");
        goto finish;
    }

#if PY_MAJOR_VERSION >= 3
    PyTuple_SetItem(pArgs, 0, PyUnicode_FromString(username));
    PyTuple_SetItem(pArgs, 1, PyUnicode_DecodeUTF8(query_str, strlen(query_str), "ignore"));
#else
    PyTuple_SetItem(pArgs, 0, PyString_FromString(username));
    PyTuple_SetItem(pArgs, 1, PyString_FromString(query_str));
#endif
    PyObject *bool_val = in_transaction ? Py_True : Py_False;
    Py_INCREF(bool_val);
    PyTuple_SetItem(pArgs, 2, bool_val);

    /* --- Call the Python function --- */
    pValue = PyObject_CallObject(pFunc, pArgs);
    if (!pValue) {
        slog_error(client, "Python Function <%s> failed to return a value", py_function);
        goto finish;
    }

    /* None is a successful no-op; errors and invalid types are failures. */
    if (pValue == Py_None) {
        *failed = false;
        goto finish;
    }
    if (PyUnicode_Check(pValue)) {
        PyObject *utf8 = PyUnicode_AsUTF8String(pValue);
        if (!utf8) goto finish;
#if PY_MAJOR_VERSION >= 3
        res = strdup(PyBytes_AsString(utf8));
#else
        res = strdup(PyString_AsString(utf8));
#endif
        Py_DECREF(utf8);
#if PY_MAJOR_VERSION >= 3
    } else if (PyBytes_Check(pValue)) {
        res = strdup(PyBytes_AsString(pValue));
#else
    } else if (PyString_Check(pValue)) {
        res = strdup(PyString_AsString(pValue));
#endif
    } else {
        PyErr_SetString(PyExc_TypeError, "RR plugin must return a string or None");
        goto finish;
    }
    if (!res) {
        PyErr_NoMemory();
        goto finish;
    }
    *failed = false;

finish:
    if (PyErr_Occurred()) {
        *failed = true;
        free(res);
        res = NULL;
        PyErr_Fetch(&ptype, &perror, &ptraceback);
        PyObject *repr = perror ? PyObject_Str(perror) : NULL;
#if PY_MAJOR_VERSION >= 3
        PyObject *utf8 = repr ? PyUnicode_AsUTF8String(repr) : NULL;
        slog_error(client, "Python error: %s", utf8 ? PyBytes_AsString(utf8) : "unavailable");
        Py_XDECREF(utf8);
#else
        slog_error(client, "Python error: %s", repr ? PyString_AsString(repr) : "unavailable");
#endif
        Py_XDECREF(repr);
        Py_XDECREF(ptype);
        Py_XDECREF(perror);
        Py_XDECREF(ptraceback);
        PyErr_Clear();
    }

    /* --- Cleanup --- */
    free(py_file_copy);
    free(file_tmp);
    free(py_path_env);
    Py_XDECREF(pName);
    Py_XDECREF(pModule);
    Py_XDECREF(pFunc);
    Py_XDECREF(pArgs);
    Py_XDECREF(pValue);
    return res;
}