/*
 * pycall.c — Compatible with Python 2.7 and Python 3.x
 * For pgbouncer-rr extension (query rewriting / routing)
 */

#include <Python.h>
#include "bouncer.h"
#include <usual/pgutil.h>

char *pycall(PgSocket *client, char *username, char *query_str, int in_transaction,
             char *py_file, char *py_function) {

    PyObject *pName = NULL, *pModule = NULL, *pFunc = NULL;
    PyObject *pArgs = NULL, *pValue = NULL;
    PyObject *ptype, *perror, *ptraceback;
    char *res = NULL, *ext = NULL, *py_file_copy = NULL, *py_module = NULL, *py_path_env = NULL;

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

    sprintf(py_path_env, "PYTHONPATH=%s", dir);
    putenv(py_path_env);

    /* --- Derive module name --- */
    char *file_tmp = strdup(py_file);
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
    PyTuple_SetItem(pArgs, 2, in_transaction ? Py_True : Py_False);
    Py_INCREF(in_transaction ? Py_True : Py_False);

    /* --- Call the Python function --- */
    pValue = PyObject_CallObject(pFunc, pArgs);
    if (!pValue) {
        slog_error(client, "Python Function <%s> failed to return a value", py_function);
        goto finish;
    }

    /* --- Convert Python return to char* --- */
#if PY_MAJOR_VERSION >= 3
    if (PyUnicode_Check(pValue)) {
        PyObject *utf8 = PyUnicode_AsUTF8String(pValue);
        res = strdup(PyBytes_AsString(utf8));
        Py_DECREF(utf8);
    } else if (PyBytes_Check(pValue)) {
        res = strdup(PyBytes_AsString(pValue));
    } else {
        res = NULL;
    }
#else
    if (PyString_Check(pValue)) {
        res = strdup(PyString_AsString(pValue));
    } else if (PyUnicode_Check(pValue)) {
        PyObject *utf8 = PyUnicode_AsUTF8String(pValue);
        res = strdup(PyString_AsString(utf8));
        Py_DECREF(utf8);
    } else {
        res = NULL;
    }
#endif

finish:
    if (PyErr_Occurred()) {
        PyErr_Fetch(&ptype, &perror, &ptraceback);
        PyObject *repr = PyObject_Str(perror);
#if PY_MAJOR_VERSION >= 3
        PyObject *utf8 = PyUnicode_AsUTF8String(repr);
        slog_error(client, "Python error: %s", PyBytes_AsString(utf8));
        Py_DECREF(utf8);
#else
        slog_error(client, "Python error: %s", PyString_AsString(repr));
#endif
        Py_XDECREF(repr);
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