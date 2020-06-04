
import argparse
import os

import yaml
from collections import defaultdict
from collections import OrderedDict

import sys
from os import path
sys.path.append(path.dirname(path.abspath(__file__)))

import cwrap_parser
import nn_parse
import native_parse
import preprocess_declarations
import function_wrapper
import gen_backend_select_register

from code_template import CodeTemplate


# This file is the top-level entry point for code generation in ATen.
# It takes an arbitrary number of arguments specifying metadata files to
# process (.cwrap, .yaml and .h) and outputs a number generated header
# and cpp files in ATen/ (see invocations of 'write' for each file that
# is written.) It is invoked from cmake; look for the 'cwrap_files'
# variable for an up-to-date list of files which are passed.

parser = argparse.ArgumentParser(description='Generate ATen source files')
parser.add_argument('files', help='cwrap files', nargs='+')

parser.add_argument(
    '-s',
    '--source-path',
    help='path to source directory for ATen',
    default='.')
parser.add_argument(
    '-o',
    '--output-dependencies',
    help='output a list of dependencies into the given file and exit')
parser.add_argument(
    '-d', '--install_dir', help='output directory', default='ATen')
parser.add_argument(
    '--rocm',
    action='store_true',
    help='reinterpret CUDA as ROCm/HIP and adjust filepaths accordingly')
parser.add_argument(
    '--vulkan',
    action='store_true',
    help='Generate Vulkan backend functions')
parser.add_argument(
    '--op_registration_whitelist',
    nargs='*',
    help='filter op registrations by the whitelist (if set); '
         'each item is `namespace`::`operator name` without overload name; '
         'e.g.: aten::empty aten::conv2d ...')
parser.add_argument(
    '--backend_whitelist',
    nargs='*',
    help='filter dispatch backend by the whitelist (if set), '
         'e.g.: CPU CUDA QuantizedCPU ...')
parser.add_argument(
    '--per_op_registration',
    action='store_true',
    help='group function registrations by op name and write to separate files; '
         'must also set --op_registration_whitelist param')
parser.add_argument(
    '--force_schema_registration',
    action='store_true',
    help='force it to generate schema-only registrations for all ops, including'
         'those that are not listed on --op_registration_whitelist')
options = parser.parse_args()

# NB: It is mandatory to NOT use os.path.join here, as the install directory
# will eventually be ingested by cmake, which does not respect Windows style
# path slashes.  If you switch this to use os.path.join, you'll get an error
# like:
#
#   Syntax error in cmake code when parsing string
#
#     C:/Jenkins/workspace/pytorch-builds/pytorch-win-ws2016-cuda9-cudnn7-py3-build/build/aten/src/ATen\core/TensorMethods.h
#
#   Invalid character escape '\c'.
core_install_dir = options.install_dir + '/core' if options.install_dir is not None else None
if options.install_dir is not None and not os.path.exists(options.install_dir):
    os.makedirs(options.install_dir)
if core_install_dir is not None and not os.path.exists(core_install_dir):
    os.makedirs(core_install_dir)


class FileManager(object):
    def __init__(self, install_dir=None):
        self.install_dir = install_dir if install_dir else options.install_dir
        self.filenames = set()
        self.outputs_written = False
        self.undeclared_files = []

    def will_write(self, filename):
        filename = '{}/{}'.format(self.install_dir, filename)
        if self.outputs_written:
            raise Exception("'will_write' can only be called before " +
                            "the call to write_outputs, refactor so outputs are registered " +
                            "before running the generators")
        self.filenames.add(filename)

    def _write_if_changed(self, filename, contents):
        try:
            with open(filename, 'r') as f:
                old_contents = f.read()
        except IOError:
            old_contents = None
        if contents != old_contents:
            with open(filename, 'w') as f:
                f.write(contents)

    def write_outputs(self, filename):
        """Write a file containing the list of all outputs which are
        generated by this script."""
        self._write_if_changed(
            filename,
            ''.join(name + ";" for name in sorted(self.filenames)))
        self.outputs_written = True

    def write(self, filename, s, env=None):
        filename = '{}/{}'.format(self.install_dir, filename)
        if isinstance(s, CodeTemplate):
            assert env is not None
            comment = "@" + "generated by aten/src/ATen/gen.py"
            if s.filename:
                comment += " from {}".format(os.path.basename(s.filename))
            env['generated_comment'] = comment
            s = s.substitute(env)
        self._write_if_changed(filename, s)
        if filename not in self.filenames:
            self.undeclared_files.append(filename)
        else:
            self.filenames.remove(filename)

    def check_all_files_written(self):
        if len(self.undeclared_files) > 0:
            raise Exception(
                "trying to write files {} which are not ".format(self.undeclared_files) +
                "in the list of outputs this script produces. " +
                "use will_write to add them.")
        if len(self.filenames) > 0:
            raise Exception("Outputs declared with 'will_write' were " +
                            "never written: {}".format(self.filenames))


TEMPLATE_PATH = options.source_path + "/templates"
TYPE_DERIVED_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDerived.cpp")
SPARSE_TYPE_DERIVED_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/SparseTypeDerived.cpp")
TYPE_DERIVED_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDerived.h")
TYPE_DEFAULT_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDefault.h")
TYPE_DEFAULT_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDefault.cpp")
OPS_ALREADY_MOVED_TO_C10_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/ATenOpList.cpp")
BACKEND_SELECT_REGISTER_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/BackendSelectRegister.cpp")
SCHEMA_REGISTER_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/SchemaRegister.cpp")
TENSOR_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TensorBody.h")
TENSOR_METHODS_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/TensorMethods.cpp")

FUNCTIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/Functions.h")
TENSOR_FUNCTIONS_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/TensorFunctions.cpp")

LEGACY_TH_FUNCTIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/LegacyTHFunctions.h")
LEGACY_TH_FUNCTIONS_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/LegacyTHFunctions.cpp")

NATIVE_FUNCTIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/NativeFunctions.h")

PER_OP_REGISTRATION_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/PerOpRegistration.cpp")

core_file_manager = FileManager(core_install_dir)
file_manager = FileManager()
cuda_file_manager = FileManager()

def backend_to_devicetype(backend):
    if backend == 'QuantizedCPU':
        return 'CPU'
    elif backend == 'QuantizedCUDA':
        return 'CUDA'
    return backend

backends = ['CPU', 'CUDA']
densities = ['Dense', 'Sparse', 'Mkldnn']  # TODO: layout instead of densities?

quantized_backends = ['QuantizedCPU', 'QuantizedCUDA']

# scalar_name, c_type, accreal, is_floating_type
quantized_scalar_types = [
    ('QInt8', 'qint8', 'QInt8AccrealNotDefined', 'QInt8IsFloatingTypeNotDefined'),
    ('QUInt8', 'quint8', 'QUInt8AccrealNotDefined', 'QUInt8IsFloatingTypeNotDefined'),
    ('QInt32', 'qint32', 'QInt32AccrealNotDefined', 'Qint32IsFloatingTypeNotDefined'),
]

# whitelist used to filter op registrations for custom build
if options.op_registration_whitelist is not None:
    op_registration_whitelist = set(options.op_registration_whitelist)
else:
    op_registration_whitelist = None

# shared environment for non-derived base classes TensorBody.h Storage.h
top_env = {
    'cpu_type_headers': [],
    'cuda_type_headers': [],
    'function_registrations': [],
    'aten_ops': [],
    'type_method_declarations': [],
    'type_method_definitions': [],
    'tensor_method_declarations': [],
    'tensor_method_definitions': [],
    'function_declarations': [],
    'function_definitions': [],
    'type_ids': [],
    'native_function_declarations': [],
}


def is_whitelisted_backend(backend):
    return options.backend_whitelist is None or backend in options.backend_whitelist

def is_cuda_backend(backend):
    return backend in ("QuantizedCUDA", "CUDA")

def dict_representer(dumper, data):
    return dumper.represent_dict(data.items())


def postprocess_output_declarations(output_declarations):
    # ensure each return has a name associated with it
    for decl in output_declarations:
        has_named_ret = False
        for n, ret in enumerate(decl.returns):
            if 'name' not in ret:
                assert not has_named_ret
                if decl.inplace:
                    ret['name'] = 'self'
                elif len(decl.returns) == 1:
                    ret['name'] = 'out'
                else:
                    ret['name'] = 'out' + str(n)
            else:
                has_named_ret = True

    def remove_key_if_none(dictionary, key):
        if key in dictionary.keys() and dictionary[key] is None:
            del dictionary[key]
        return dictionary

    return [remove_key_if_none(decl._asdict(), 'buffers')
            for decl in output_declarations]


def format_yaml(data):
    if options.output_dependencies:
        # yaml formatting is slow so don't do it if we will ditch it.
        return ""
    noalias_dumper = yaml.dumper.SafeDumper
    noalias_dumper.ignore_aliases = lambda self, data: True
    # Support serializing OrderedDict
    noalias_dumper.add_representer(OrderedDict, dict_representer)
    # Some yaml parsers (e.g. Haskell's) don't understand line breaks.
    # width=float('Inf') turns off optional line breaks and improves
    # the portability of the outputted yaml.
    return yaml.dump(data, default_flow_style=False, Dumper=noalias_dumper, width=float('Inf'))


def add_op_registrations(per_type_registrations, per_op_registrations, schema_registrations, op_registrations):
    for op_registration in op_registrations:
        opname = op_registration.operator_name
        registration = op_registration.registration_code

        # collect schema registration for all ops (whitelisted or not)
        if schema_registrations is not None:
            schema_registrations.append(op_registration.schema_registration_code)

        # apply whitelist
        if op_registration_whitelist is not None and opname not in op_registration_whitelist:
            continue
        if options.per_op_registration:
            # per op registration
            per_op_registrations[opname].append(registration)
        else:
            # per type registration
            per_type_registrations.append(registration)


def generate_storage_type_and_tensor(backend, density, declarations, per_op_registrations, schema_registrations):
    env = {}
    density_tag = density if density != 'Dense' else ''
    env['Density'] = density
    env['Type'] = "{}{}Type".format(density_tag, backend)
    env['DeviceType'] = backend_to_devicetype(backend)
    env['Backend'] = density_tag + backend
    if not is_whitelisted_backend(env['Backend']):
        return
    env['storage_tensor_headers'] = []
    if density != 'Sparse':
        env['storage_tensor_headers'] = ['#include <c10/core/TensorImpl.h>']

    # used for generating switch logic for external functions
    tag = density_tag + backend
    env['TypeID'] = 'TypeID::' + tag
    top_env['type_ids'].append(tag + ',')

    env['legacy_th_headers'] = []
    if is_cuda_backend(backend):
        env['extra_cuda_headers'] = []
        env['extra_cuda_headers'].append('#include <ATen/DeviceGuard.h>')
        if options.rocm:
            env['th_headers'] = [
                '#include <THH/THH.h>',
                '#include <THH/THHTensor.hpp>',
                '#include <THHUNN/THHUNN.h>',
                '#undef THNN_',
                '#undef THCIndexTensor_',
            ]
            env['extra_cuda_headers'].append('#include <ATen/hip/ATenHIPGeneral.h>')
            env['extra_cuda_headers'].append('#include <ATen/hip/HIPDevice.h>')
            env['extra_cuda_headers'].append('#include <ATen/hip/HIPContext.h>')
        else:
            env['th_headers'] = [
                '#include <THC/THC.h>',
                '#include <THC/THCTensor.hpp>',
                '#include <THCUNN/THCUNN.h>',
                '#undef THNN_',
                '#undef THCIndexTensor_',
            ]
            env['extra_cuda_headers'].append('#include <ATen/cuda/ATenCUDAGeneral.h>')
            env['extra_cuda_headers'].append('#include <ATen/cuda/CUDADevice.h>')
            env['extra_cuda_headers'].append('#include <ATen/cuda/CUDAContext.h>')
        env['state'] = ['globalContext().getTHCState()']
        env['isCUDA'] = 'true'
        env['storage_device'] = 'return storage->device;'
        env['Generator'] = 'CUDAGeneratorImpl'
        env['allocator'] = 'at::cuda::getCUDADeviceAllocator()'
    else:
        env['th_headers'] = [
            '#include <TH/TH.h>',
            '#include <TH/THTensor.hpp>',
        ]
        env['extra_cuda_headers'] = []
        env['state'] = []
        env['isCUDA'] = 'false'
        env['storage_device'] = 'throw std::runtime_error("CPU storage has no device");'
        env['Generator'] = 'CPUGeneratorImpl'
        env['allocator'] = 'getCPUAllocator()'

    declarations, definitions, op_registrations, th_declarations, th_definitions = function_wrapper.create_derived(
        env, declarations)
    env['type_derived_method_declarations'] = declarations
    env['type_derived_method_definitions'] = definitions
    env['legacy_th_declarations'] = th_declarations
    env['legacy_th_definitions'] = th_definitions
    env['function_registrations'] = []
    add_op_registrations(env['function_registrations'], per_op_registrations, schema_registrations, op_registrations)

    fm = file_manager
    if env['DeviceType'] == 'CUDA':
        fm = cuda_file_manager

    if env['Backend'] == 'CPU' or env['Backend'] == 'CUDA':
        env['namespace'] = env['Backend'].lower()
        env['legacy_th_headers'].append('#include <ATen/LegacyTHFunctions' + env['Backend'] + ".h>")
        fm.write('LegacyTHFunctions' + env['Backend'] + ".h", LEGACY_TH_FUNCTIONS_H, env)
        fm.write('LegacyTHFunctions' + env['Backend'] + ".cpp", LEGACY_TH_FUNCTIONS_CPP, env)

    if density != 'Sparse':
        fm.write(env['Type'] + ".cpp", TYPE_DERIVED_CPP, env)
    else:
        fm.write(env['Type'] + ".cpp", SPARSE_TYPE_DERIVED_CPP, env)
    fm.write(env['Type'] + ".h", TYPE_DERIVED_H, env)

    if env['DeviceType'] == 'CPU' or env['DeviceType'] == 'Vulkan':
        top_env['cpu_type_headers'].append(
            '#include <ATen/{}.h>'.format(env['Type']))
    else:
        assert env['DeviceType'] == 'CUDA'
        top_env['cuda_type_headers'].append(
            '#include <ATen/{}.h>'.format(env['Type']))


# yields (backend, density) tuples
def iterate_types():
    for backend in backends:
        for density in densities:
            if density == 'Mkldnn' and backend != 'CPU':
                continue
            else:
                yield (backend, density)
    for backend in quantized_backends:
        yield (backend, 'Dense')
    if options.vulkan:
        yield('Vulkan', 'Dense')


def gen_per_op_registration_filename(opname):
    return 'pt_op_register_{}.cpp'.format(opname.replace(':', '-'))


###################
# declare what files will be output _before_ we do any work
# so that the script runs quickly when we are just querying the
# outputs
def declare_outputs():
    core_files = ['TensorBody.h', 'TensorMethods.cpp', 'ATenOpList.cpp', 'TensorFunctions.cpp']
    for f in core_files:
        core_file_manager.will_write(f)
    files = ['Declarations.yaml', 'TypeDefault.cpp', 'TypeDefault.h',
             'Functions.h', 'NativeFunctions.h', 'BackendSelectRegister.cpp']
    for f in files:
        file_manager.will_write(f)
    for backend, density in iterate_types():
        full_backend = backend if density == "Dense" else density + backend
        if not is_whitelisted_backend(full_backend):
            continue
        fm = file_manager
        if is_cuda_backend(backend):
            fm = cuda_file_manager
        for kind in ["Type"]:
            if kind != 'Type' and density == "Sparse":
                # No Storage or Tensor for sparse
                continue
            fm.will_write("{}{}.h".format(full_backend, kind))
            fm.will_write("{}{}.cpp".format(full_backend, kind))
        if backend == 'CPU' or backend == 'CUDA':
            fm.will_write("LegacyTHFunctions{}.h".format(backend))
            fm.will_write("LegacyTHFunctions{}.cpp".format(backend))

    if options.per_op_registration:
        if op_registration_whitelist is None:
            raise Exception("Must set --op_registration_whitelist for per-op registration.")
        for whitelisted_op in op_registration_whitelist:
            fname = gen_per_op_registration_filename(whitelisted_op)
            file_manager.will_write(fname)

    if options.force_schema_registration:
        file_manager.will_write('SchemaRegister.cpp')


def filter_by_extension(files, *extensions):
    filtered_files = []
    for file in files:
        for extension in extensions:
            if file.endswith(extension):
                filtered_files.append(file)
    return filtered_files


def generate_per_op_registration(per_op_registrations):
    if not options.per_op_registration:
        return

    # Ensure all whitelisted operators have a corresponding registration file.
    # Generate an empty placeholder file for nonexistent operators, which might
    # be registered manually instead of via codegen.
    # This can simplify the custom BUCK build which consumes the output of this
    # script, since it can uniformly create per-op build targets and dependencies
    # without having to know the subtle difference about op registration.
    # Manually registered operators might call codegen registered operators thus
    # we cannot simply ignore them when calculating transitive dependencies for
    # custom build.
    for whitelisted_op in op_registration_whitelist:
        if whitelisted_op not in per_op_registrations:
            per_op_registrations[whitelisted_op] = []

    for opname, function_registrations in per_op_registrations.items():
        fname = gen_per_op_registration_filename(opname)
        file_manager.write(fname, PER_OP_REGISTRATION_CPP, {
            'extra_headers': top_env['cpu_type_headers'] + top_env['cuda_type_headers'],
            'function_registrations': function_registrations,
        })


def generate_schema_registration(schema_registrations):
    if not options.force_schema_registration:
        return
    file_manager.write('SchemaRegister.cpp', SCHEMA_REGISTER_CPP, {
        'schema_registrations': sorted(set(schema_registrations)),
    })


def generate_outputs():
    cwrap_files = filter_by_extension(options.files, '.cwrap')
    nn_files = filter_by_extension(options.files, 'nn.yaml', '.h')
    native_files = filter_by_extension(options.files, 'native_functions.yaml')

    declarations = [d
                    for file in cwrap_files
                    for d in cwrap_parser.parse(file)]

    declarations += nn_parse.run(nn_files)
    declarations += native_parse.run(native_files)
    declarations = preprocess_declarations.run(declarations)
    per_op_registrations = defaultdict(list) if options.per_op_registration else None
    schema_registrations = [] if options.force_schema_registration else None

    # note: this will fill in top_env['type/tensor_method_declarations/definitions']
    # and modify the declarations to include any information that will all_backends
    # be used by function_wrapper.create_derived
    output_declarations, op_registrations = function_wrapper.create_generic(
        top_env, declarations)
    output_declarations = postprocess_output_declarations(output_declarations)
    file_manager.write("Declarations.yaml", format_yaml(output_declarations))

    gen_backend_select_register.register_backend_select_methods(declarations, BACKEND_SELECT_REGISTER_CPP, file_manager)

    add_op_registrations(
        top_env['function_registrations'], per_op_registrations, schema_registrations, op_registrations)

    for backend, density in iterate_types():
        generate_storage_type_and_tensor(
            backend, density, declarations, per_op_registrations, schema_registrations)

    core_files = {
        'TensorBody.h': TENSOR_H,
        'TensorMethods.cpp': TENSOR_METHODS_CPP,
        'ATenOpList.cpp': OPS_ALREADY_MOVED_TO_C10_CPP,
    }

    for core_file, core_template_file in core_files.items():
        core_file_manager.write(core_file, core_template_file, top_env)

    file_manager.write('TypeDefault.h', TYPE_DEFAULT_H, top_env)
    file_manager.write('TypeDefault.cpp', TYPE_DEFAULT_CPP, top_env)

    file_manager.write('Functions.h', FUNCTIONS_H, top_env)
    core_file_manager.write('TensorFunctions.cpp', TENSOR_FUNCTIONS_CPP, top_env)

    file_manager.write('NativeFunctions.h', NATIVE_FUNCTIONS_H, top_env)

    generate_per_op_registration(per_op_registrations)
    generate_schema_registration(schema_registrations)

    file_manager.check_all_files_written()
    cuda_file_manager.check_all_files_written()

declare_outputs()
if options.output_dependencies is not None:
    file_manager.write_outputs(options.output_dependencies)
    core_file_manager.write_outputs(options.output_dependencies + "-core")
    cuda_file_manager.write_outputs(options.output_dependencies + "-cuda")
else:
    generate_outputs()
