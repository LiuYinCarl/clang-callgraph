# clang-callgraph
A Python 3 script based on clang which generates a call graph from a given C++ codebase.

## Installation

If you use Ubuntu or other linux operator system, install **libclang-dev(version 14)** first.

```
apt install libclang-dev
```

Clone the repository, navigate to the repository folder and install it as a Python package:
```
git clone https://github.com/LiuYinCarl/clang-callgraph.git
cd ./clang-callgraph
pip install .
```

## screenshot

![image](https://github.com/user-attachments/assets/2a58ad53-13ee-491e-a8c6-0e0840166c41)
![image](https://github.com/user-attachments/assets/3f9aeb64-f587-44ee-b3bc-6e914aac26da)
![image](https://github.com/user-attachments/assets/afd39637-4af6-4a93-9efd-2615061dfdd7)

## Fast Usage Example

Suppose we want to analysis CPython source code.


```bash
cd cpython/

./configure

# use bera to generate compile_commands.json
bear -- make -j4

clang-callgraph ./compile_commands.json

>>> @
Usage:
    @ ignore keyword1 [keyword2] ...    add ignore keywords
    @ filter keyword1 [keyword2] ...    add filter keywords
    @ del_ig keyword1 [keyword2] ...    del ignore keywords
    @ del_fi keyword1 [keyword2] ...    del filter keywords
    @ depth  n                          set max print depth
    @ show                              show query config
    @ reset                             reset query config

# do some setting
>>> @ show
filter set: set()
ignore set: {'Head', 'Inter', 'Thread', 'GC', 'REF'}
print depth: 2
max print depth: 15

# query keyword PyList
>>> PyList
matching:
PyList_New(Py_ssize_t)
PyList_Size(PyObject *)
PyList_GetItem(PyObject *, Py_ssize_t)
PyList_GetItemRef(PyObject *, Py_ssize_t)
PyList_SetItem(PyObject *, Py_ssize_t, PyObject *)
PyList_Insert(PyObject *, Py_ssize_t, PyObject *)
PyList_Append(PyObject *, PyObject *)
PyList_GetSlice(PyObject *, Py_ssize_t, Py_ssize_t)
PyList_SetSlice(PyObject *, Py_ssize_t, Py_ssize_t, PyObject *)
PyList_Sort(PyObject *)
PyList_Reverse(PyObject *)
PyList_AsTuple(PyObject *)
PyList_GET_SIZE(PyObject *)
PyList_SET_ITEM(PyObject *, Py_ssize_t, PyObject *)
PyList_Extend(PyObject *, PyObject *)
PyList_Clear(PyObject *)

# show call graph with ignore some keywords
>>> ! PyList_New(Py_ssize_t)
PyList_New(Py_ssize_t)
|--_PyFreeList_Pop(struct _Py_freelist *)
|  |--_PyFreeList_PopNoStats(struct _Py_freelist *)
|  |--_Py_NewReference(PyObject *)
|  |  |--new_reference(PyObject *)
|--_Py_freelists_GET()
|--PyMem_Calloc(size_t, size_t)
|  |--calloc
|--PyErr_NoMemory()
|--Py_SET_SIZE(PyVarObject *, Py_ssize_t)

# add filter keyword
>>> @ filter reference
update filter set: {'reference'}

# show call graph with some filter words
>>> ? PyList_New(Py_ssize_t)
PyList_New(Py_ssize_t)
|--_PyFreeList_Pop(struct _Py_freelist *)
|  |--_Py_NewReference(PyObject *)
|  |  |--new_reference(PyObject *)
|--_PyFreeList_Pop(struct _Py_freelist *)
|  |--_Py_NewReference(PyObject *)
|  |  |--new_reference(PyObject *)
|--_PyFreeList_Pop(struct _Py_freelist *)
|  |--_Py_NewReference(PyObject *)
|  |  |--new_reference(PyObject *)
|--_PyFreeList_Pop(struct _Py_freelist *)
|  |--_Py_NewReference(PyObject *)
|  |  |--new_reference(PyObject *)
|--_PyFreeList_Pop(struct _Py_freelist *)
|  |--_Py_NewReference(PyObject *)
|  |  |--new_reference(PyObject *)
|--_PyFreeList_Pop(struct _Py_freelist *)
|  |--_Py_NewReference(PyObject *)
|  |  |--new_reference(PyObject *)
```

## Usage
This is the general script usage:<br/>
`clang-callgraph file.cpp|compile_commands.json [options] [extra clang args...]`

Understood `options` are:
* `-x name1,name2`: a comma separated list of excluded prefixes, like
  `std::,boost::`. All symbols starting with one of those will be hidden in the
  callgraph.
* `-p path1,path2`: a comma separated list of excluded prefixes, like
  `/usr`. All symbols defined or used in files whose name starts with one of those
  will be hidden in the callgraph.
* `--cfg config_file`: Retrieve additional configuration from a config file.
* `--lookup function_name`: Directly lookup the given function and print its callgraph
  instead of asking the user to type a function name.

For more details look at the [example](#Example) provided.

## Configuration File
A configuration file can be used for options that don't depend on the source project to be analysed.
This way it makes command lines shorter.
The format used is YAML and may contain the following entries :
* `excluded_prefixes`: same as the -x option
* `excluded_paths`: same as the -p option
* `clang_args`: any additional clang args

## Dependencies
This script is based on Python 3, therefore a compatible interpreter is needed.<br/>
Clang is also needed, below here an example of the required packaged supposing Clang 14 is available on an Ubuntu installation, adapt the following depending on your distro and packages availability:
```
$ sudo apt install clang-14 libclang-14-dev
$ pip install clang==14.0.0
```

## Generating compile_commands.json
The `compile_commands.json` is a compilation database formatted in JSON, and contains details and flags for each compilation unit (.c/.cpp file) of your project.<br/>
For more details look [here](https://clang.llvm.org/docs/JSONCompilationDatabase.html).

### CMake based projects
Simply enable the [CMAKE_EXPORT_COMPILE_COMMANDS](https://cmake.org/cmake/help/latest/variable/CMAKE_EXPORT_COMPILE_COMMANDS.html)
setting when configuring your project and CMake will automatically generate a `compile_commands.json` file inside your chosen build folder.

### Make based projects
The easiest way to generate the file compile\_commands.json for any make based
compilation chain is to use [Bear](https://github.com/rizsotto/Bear) and recompile
with `bear -- make`.

## Example
When running the python script, after parsing all the codebase, you are
prompted to type in the function's name for which you want to obtain the
callgraph:
```
$ clang-callgraph compile_commands.json -p /usr/lib/llvm-14/lib/clang/14.0.0/include/
reading source files...
/home/vermeille/CPAsim/src/module.cpp
/home/vermeille/CPAsim/src/module/modulevalues.cpp
/home/vermeille/CPAsim/src/main.cpp
/home/vermeille/CPAsim/src/parser.cpp
> main
matching:
main(int, char **)
> main(int, char **)
main(int, char **)
  Parser::ParseModuleDef(std::istream &)
    Parser::EatWord(std::istream &, const std::string &)
      Parser::FuckSpaces(std::istream &)
      Parser::EatChar(std::istream &, char)
    Parser::ParseWord(std::istream &)
      Parser::FuckSpaces(std::istream &)
    Module::Module(const std::string &)
    Parser::FuckSpaces(std::istream &)
    Parser::EatChar(std::istream &, char)
    Parser::FuckSpaces(std::istream &)
    Parser::EatChar(std::istream &, char)
    Module::AddInput(std::unique_ptr<WireDecl>)
      WireDecl::name()
      WireDecl::name()
      WireDecl::name()
    Parser::ParseWireDecl(std::istream &)
      Parser::ParseWord(std::istream &)
      Parser::FuckSpaces(std::istream &)
      Parser::EatChar(std::istream &, char)
      Parser::FuckSpaces(std::istream &)
      Parser::ParseDecimalInt(std::istream &)
        Parser::FuckSpaces(std::istream &)
      Parser::FuckSpaces(std::istream &)
      Parser::EatChar(std::istream &, char)
      Parser::FuckSpaces(std::istream &)
      WireDecl::WireDecl(const std::string &, int)
```
```
$ clang-callgraph compile_commands.json -x Parser:: -p /usr/lib/llvm-14/lib/clang/14.0.0/include/
reading source files...
/home/vermeille/CPAsim/src/module.cpp
/home/vermeille/CPAsim/src/module/modulevalues.cpp
/home/vermeille/CPAsim/src/main.cpp
/home/vermeille/CPAsim/src/parser.cpp
> main(int, char **)
main(int, char **)
  Module::BindUsagesToDef()
    Module::BindUsagesToDef_Rec(Expr *)
      Module::BindUsagesToDef_Rec(Expr *)
        Module::BindUsagesToDef_Rec(Expr *)
        Binop::lhs()
        Module::BindUsagesToDef_Rec(Expr *)
        Binop::rhs()
        Module::BindUsagesToDef_Rec(Expr *)
        Not::rhs()
        WireUsage::name()
        WireUsage::name()
        WireUsage::SetDeclRef(WireDecl *)
        WireUsage::IsUseValid()
          WireDecl::size()
          WireDecl::size()
          WireDecl::size()
        WireUsage::name()
        WireUsage::index()
        WireUsage::index()
        WireDecl::size()
```

Configuration file example :
callgraph.yml:
```
excluded_prefixes:
  - 'std::'
excluded_paths:
  - /usr/include
  - /usr/lib/llvm-14/lib/clang/14.0.0/include/
clang_args:
  - '-I/usr/lib/gcc/x86_64-redhat-linux/12/include'
```
