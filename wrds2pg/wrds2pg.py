# Run the SAS code on the WRDS server and get the result
import pandas as pd
from io import StringIO
import re, subprocess, os, paramiko
from time import gmtime, strftime
from sqlalchemy import create_engine, inspect
from sqlalchemy import text
import duckdb
from pathlib import Path
import shutil
import gzip
import tempfile
import pyarrow.parquet as pq
import pyarrow as pa
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy.engine import reflection
from os import getenv

client = paramiko.SSHClient()
wrds_id = getenv("WRDS_ID")

import warnings
warnings.filterwarnings(action='ignore', module='.*paramiko.*')

def get_process(sas_code, wrds_id=None, fpath=None):

    if client:
        client.close()
        
    if wrds_id:
        """Function runs SAS code on WRDS server and
        returns result as pipe on stdout."""
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.WarningPolicy())
        client.connect('wrds-cloud-sshkey.wharton.upenn.edu',
                       username=wrds_id, compress=False)
        command = "qsas -stdio -noterminal"
        stdin, stdout, stderr = client.exec_command(command)
        stdin.write(sas_code)
        stdin.close()

        channel = stdout.channel
        # indicate that we're not going to write to that channel anymore
        channel.shutdown_write()
        return stdout

    elif fpath:

        p=subprocess.Popen(['sas', '-stdio', '-noterminal'],
                           stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        p.stdin.write(sas_code)
        p.stdin.close()

        return p.stdout

def code_row(row):

    """A function to code PostgreSQL data types using output from SAS's PROC CONTENTS."""

    format_ = row['format']
    formatd = row['formatd']
    formatl = row['formatl']
    col_type = row['type']

    if col_type==2:
        return 'text'

    if not pd.isnull(format_):
        if re.search(r'datetime', format_, re.I):
            return 'timestamp'
        elif format_ =='TIME8.' or re.search(r'time', format_, re.I) or format_=='TOD':
            return "time"
        elif re.search(r'(date|yymmdd|mmddyy)', format_, re.I):
            return "date"

    if format_ == "BEST":
        return 'float8'
    if formatd != 0:
        return 'float8'
    if formatd == 0 and formatl != 0:
        return 'int8'
    if formatd == 0 and formatl == 0:
        return 'float8'
    else:
        return 'text'

################################################
# 1. Get format of variables on WRDS table     #
################################################
# SAS code to extract information about the datatypes of the SAS data.
# Note that there are some date formates that don't work with this code.
def get_row_sql(row):
    """Function to get SQL to create column from row in PROC CONTENTS."""
    postgres_type = row['postgres_type']
    if postgres_type == 'timestamp':
        postgres_type = 'text'

    return '"' + row['name'].lower() + '" ' + postgres_type

def sas_to_pandas(sas_code, wrds_id, fpath=None, encoding=None):

    """Function that runs SAS code on WRDS or local server
    and returns a Pandas data frame."""
    if not encoding:
        encoding = "utf-8"
    
    p = get_process(sas_code, wrds_id, fpath)

    if wrds_id:
        df = pd.read_csv(StringIO(p.read().decode(encoding)))
    else:
        df = pd.read_csv(StringIO(p.read()))
    df.columns = map(str.lower, df.columns)
    p.close()

    return(df)

def make_sas_code(table_name, schema, wrds_id=None, fpath=None, \
                  rpath=None, drop="", keep="", rename="", \
                  alt_table_name=None, sas_schema=None):
    if not sas_schema:
        sas_schema = schema
        
    if fpath:
        libname_stmt = "libname %s '%s';" % (sas_schema, fpath)
        
    elif rpath:
        libname_stmt = "libname %s '%s';" % (sas_schema, rpath)
    else:
        libname_stmt = ""

    sas_template = """
        options nonotes nosource;
        %s

        * Use PROC CONTENTS to extract the information desired.;
        proc contents data=%s.%s(%s %s obs=1 %s) out=schema noprint;
        run;

        proc sort data=schema;
            by varnum;
        run;

        * Now dump it out to a CSV file;
        proc export data=schema(keep=name format formatl 
                                      formatd length type)
            outfile=stdout dbms=csv;
        run;
    """

    if rename != '':
        rename_str = "rename=(" + rename + ") "
    else:
        rename_str = ""

    if drop != '':
        drop_str = "drop=" + drop 
    else:
        drop_str = ""
        
    if keep != '':
        keep_str = "keep=" + keep 
    else:
        keep_str = ""

    sas_code = sas_template % (libname_stmt, sas_schema, table_name, drop_str,
                               keep_str, rename_str)
    return sas_code
                             

def get_table_sql(table_name, schema, wrds_id=None, fpath=None, \
                  rpath=None, drop="", keep="", rename="", return_sql=True, \
                  alt_table_name=None, col_types=None, sas_schema=None):
                      
    if not alt_table_name:
        alt_table_name = table_name
    
    if not col_types:
        col_types = {}
        
    sas_code = make_sas_code(table_name=table_name, \
                             schema = schema, wrds_id=wrds_id, fpath=fpath, \
                  rpath=rpath, drop=drop, keep=keep, rename=rename, \
                  alt_table_name=alt_table_name, sas_schema=sas_schema)
    
    # Run the SAS code on the WRDS server and get the result
    df = sas_to_pandas(sas_code, wrds_id, fpath)
    
    # Make all variable names lower case, get
    # inferred types, then set explicit types if given
    # Identify the datetime fields. These need special handling.
    df['name'] = df['name'].str.lower()
    df['postgres_type'] = df.apply(code_row, axis=1)
    datetimes_inferred = df.loc[df['postgres_type']=="timestamp", "name"]
    datetimes_inferred_cols = [field.lower() for field in datetimes_inferred
                                 if field not in col_types.keys()]
    datetimes_specified = [col for col in col_types if col_types[col]=='timestamp']
    datetimes = list(set(datetimes_inferred_cols + datetimes_specified))
    
    if col_types:
        for var in col_types.keys():
            df.loc[df.name == var, 'postgres_type'] = col_types[var]
    rows_str = df.apply(get_row_sql, axis=1).str.cat(sep=", ")
    make_table_sql = "CREATE TABLE " + schema + "." + alt_table_name + " (" + \
                      rows_str + ")"
    
    if return_sql:
        return {"sql": make_table_sql, "datetimes": datetimes}
    else:
        df['name'] = df['name'].str.lower()
        return df

def get_wrds_sas(table_name, schema, wrds_id=None, fpath=None, rpath=None,
                     drop="", keep="", fix_cr = False, 
                     fix_missing = False, obs="", rename="",
                     encoding=None, sas_encoding=None):
    if fix_cr:
        fix_missing = True;
        fix_cr_code = """
            * fix_cr_code;
            array _char _character_;
            
            do over _char;
                _char = compress(_char, , 'kw');
            end;"""
    else:
        fix_cr_code = ""

    if fpath:
        libname_stmt = "libname %s '%s';" % (schema, fpath)
    elif rpath:
        libname_stmt = "libname %s '%s';" % (schema, rpath)
    else:
        libname_stmt = ""

    if rename != '':
        rename_str = " rename=(" + rename + ")"
    else:
        rename_str = ""
        
    if not sas_encoding:
        sas_encoding_str=""
    else:
        sas_encoding_str="(encoding='" + sas_encoding + "')"

    if fix_missing or drop != '' or obs != '' or keep !='':
        # If need to fix special missing values, then convert them to
        # regular missing values, then run PROC EXPORT
        if table_name == "dsf":
            dsf_fix = """
                * dsf_fix;
                format numtrd 8.;\n"""
        else:
            dsf_fix = ""

        if obs != "":
            obs_str = " obs=" + str(obs)
        else:
            obs_str = ""

        if drop != "":
            drop_str = "drop=" + drop + " "
        else:
            drop_str = ""
        
        if keep != "":
            keep_str = "keep=" + keep + " "
        else:
            keep_str = ""
        
        if keep:
            print(keep_str)
        
        if obs != '' or drop != '' or rename != '' or keep != '':
            sas_table = table_name + "(" + drop_str + keep_str + \
                                           obs_str + rename_str + ")"
        else:
            sas_table = table_name

        if table_name == "fund_names":
            fund_names_fix = """
                proc sql;
                    DELETE FROM %s%s
                    WHERE prxmatch('\\D', first_offer_dt) ge 1;
                quit;""" % (wrds_id, table_name)
        else:
            fund_names_fix = ""

        # Cut table name to no more than 32 characters
        # (A SAS limitation)
        new_table = "%s%s" % (schema, table_name)
        new_table = new_table[0:min(len(new_table), 32)]
        
        if fix_missing:
            fix_missing_str = """
                * fix_missing code;
                array allvars _numeric_ ;

                do over allvars;
                  if missing(allvars) then allvars = . ;
                end;"""
        else:
            fix_missing_str = ""
        
        sas_template = """
            options nosource nonotes;

            %s

            * Fix missing values;
            data %s;
                set %s.%s%s; 

                %s
                
                %s

                %s
            run;

            * fund_names_fix;
            %s

            proc export data=%s(encoding="wlatin1") outfile=stdout dbms=csv;
            run;"""
        sas_code = sas_template % (libname_stmt, new_table, 
                                   schema, sas_table, sas_encoding_str, dsf_fix,
                                   fix_cr_code, fix_missing_str, fund_names_fix, new_table)
                              
    else:

        sas_template = """
            options nosource nonotes;
            %s

            proc export data=%s.%s(%s encoding="wlatin1") outfile=stdout dbms=csv;
            run;"""

        sas_code = sas_template % (libname_stmt, schema, table_name, rename_str) 
    return sas_code        
    
def get_wrds_process(table_name, schema, wrds_id=None, fpath=None, rpath=None,
                     drop="", keep="", fix_cr = False, 
                     fix_missing = False, obs="", rename="",
                     encoding=None, sas_encoding=None):
    sas_code = get_wrds_sas(table_name=table_name, wrds_id=wrds_id,
                                    rpath=rpath, fpath=fpath, schema=schema, 
                                    drop=drop, rename=rename, keep=keep, fix_cr=fix_cr,
                                    fix_missing=fix_missing, obs=obs, encoding=encoding,
                                    sas_encoding=sas_encoding)
    
    p = get_process(sas_code, wrds_id=wrds_id, fpath=fpath)
    return(p)

def wrds_to_pandas(table_name, schema, wrds_id, rename="", 
                   drop="", obs=None, encoding=None, fpath=None, rpath=None,
                   sas_schema=None):

    if not encoding:
        encoding = "utf-8"

    if not sas_schema:
        sas_schema = schema

    p = get_wrds_process(table_name, sas_schema, wrds_id, drop=drop, rename=rename, obs=obs,
                         fpath=fpath, rpath=rpath)
    df = pd.read_csv(StringIO(p.read().decode(encoding)))
    df.columns = map(str.lower, df.columns)
    p.close()

    return(df)

def get_modified_str(table_name, sas_schema, wrds_id, encoding=None, rpath=None):
    
    if not encoding:
        encoding = "utf-8"

    if not rpath:
        rpath = sas_schema
    
    sas_code = "proc contents data=" + rpath + "." + table_name + "(encoding='wlatin1');"

    p = get_process(sas_code, wrds_id)
    contents = p.readlines()
    modified = ""

    next_row = False
    for line in contents:
        if next_row:
            line = re.sub(r"^\s+(.*)\s+$", r"\1", line)
            line = re.sub(r"\s+$", "", line)
            if not re.findall(r"Protection", line):
                modified += " " + line.rstrip()
            next_row = False

        if re.match(r"Last Modified", line):
            modified = re.sub(r"^Last Modified\s+(.*?)\s{2,}.*$", r"Last modified: \1", line)
            modified = modified.rstrip()
            next_row = True

    return modified

def get_table_comment(table_name, schema, engine):

    if engine.dialect.has_table(engine.connect(), table_name, schema=schema):
        sql = """SELECT obj_description('"%s"."%s"'::regclass, 'pg_class')""" % (schema, table_name)
        with engine.connect() as conn:
            res = conn.execute(text(sql)).fetchone()[0]
        return(res)
    else:
        return ""
        
def set_table_comment(table_name, schema, comment, engine):

    connection = engine.connect()
    trans = connection.begin()
    sql = """
        COMMENT ON TABLE "%s"."%s" IS '%s'""" % (schema, table_name, comment)

    try:
        res = connection.execute(text(sql))
        trans.commit()
    except:
        trans.rollback()
        raise

    return True

def wrds_to_pg(table_name, schema, engine, wrds_id=None,
               fpath=None, rpath=None, fix_missing=False, fix_cr=False, 
               drop="", obs="", rename="", keep="",
               alt_table_name = None, encoding=None, col_types=None, create_roles=True,
               sas_schema=None, sas_encoding=None):

    if not alt_table_name:
        alt_table_name = table_name
    
    if not sas_schema:
        sas_schema = schema
        
    make_table_data = get_table_sql(table_name=table_name, wrds_id=wrds_id,
                                    rpath=rpath, fpath=fpath, schema=schema, 
                                    drop=drop, rename=rename, keep=keep,
                                    alt_table_name=alt_table_name, col_types=col_types,
                                    sas_schema=sas_schema)

    process_sql("DROP TABLE IF EXISTS " + schema + "." + alt_table_name + " CASCADE", engine)
        
    # Create schema (and associated role) if necessary
    insp = inspect(engine)
    if not schema in insp.get_schema_names():
        process_sql("CREATE SCHEMA " + schema, engine)
            
        if create_roles:
            if not role_exists(engine, schema):
                create_role(engine, schema)
            process_sql("ALTER SCHEMA " + schema + " OWNER TO " + schema, engine)
            if not role_exists(engine, "%s_access" % schema):
                create_role(engine, "%s_access" % schema)
            process_sql("GRANT USAGE ON SCHEMA " + schema + " TO " + 
                         schema + "_access", engine)
    process_sql(make_table_data["sql"], engine)
        
    now = strftime("%H:%M:%S", gmtime())
    print("Beginning file import at %s." % now)
    print("Importing data into %s.%s" % (schema, alt_table_name))
    p = get_wrds_process(table_name=table_name, fpath=fpath, rpath=rpath,
                                 schema=sas_schema, wrds_id=wrds_id,
                                 drop=drop, keep=keep, fix_cr=fix_cr, fix_missing=fix_missing, 
                                 obs=obs, rename=rename, sas_encoding=sas_encoding)

    res = wrds_process_to_pg(alt_table_name, schema, engine, p, encoding)
    now = strftime("%H:%M:%S", gmtime())
    print("Completed file import at %s." % now)

    for var in make_table_data["datetimes"]:
        print("Fixing %s" % var)
        sql = r"""
            ALTER TABLE "%s"."%s"
            ALTER %s TYPE timestamp
            USING regexp_replace(%s, '(\d{2}[A-Z]{3}\d{2,4}):?(.*$)', '\1 \2' )::timestamp""" % (schema, alt_table_name, var, var)
        process_sql(sql, engine)

    return res

def wrds_process_to_pg(table_name, schema, engine, p, encoding=None):
    # The first line has the variable names ...
    
    if not encoding:
        encoding = "UTF8"
    
    var_names = p.readline().rstrip().lower().split(sep=",")
    
    # ... the rest is the data
    copy_cmd =  "COPY " + schema + "." + table_name + ' ("' + '", "'.join(var_names) + '")'
    copy_cmd += " FROM STDIN CSV ENCODING '%s'" % encoding
    
    with engine.connect() as conn:
        connection_fairy = conn.connection
        try:
            with connection_fairy.cursor() as curs:
                curs.execute("SET DateStyle TO 'ISO, MDY'")
                curs.copy_expert(copy_cmd, p)
                curs.close()
        finally:
            connection_fairy.commit()
            conn.close()
            p.close()
    return True

def wrds_update(table_name, schema, host=os.getenv("PGHOST"), dbname=os.getenv("PGDATABASE"), engine=None, 
        wrds_id=os.getenv("WRDS_ID"), rpath=None, fpath=None, force=False, fix_missing=False, fix_cr=False, drop="", keep="", 
        obs="", rename="", alt_table_name=None, encoding=None, col_types=None, create_roles=True,
        sas_schema=None, sas_encoding=None):
          
    if not sas_schema:
        sas_schema = schema
        
    if not alt_table_name:
        alt_table_name = table_name
        
    if not engine:
        if not (host and dbname):
            print("Error: Missing connection variables. Please specify engine or (host, dbname).")
            quit()
        else:
            engine = create_engine("postgresql://" + host + "/" + dbname) 
    if wrds_id:
        # 1. Get comments from PostgreSQL database
        comment = get_table_comment(alt_table_name, schema, engine)
        
        # 2. Get modified date from WRDS
        modified = get_modified_str(table_name, sas_schema, wrds_id, 
                                    encoding=encoding, rpath=rpath)
    else:
        comment = 'Updated on ' + strftime("%Y-%m-%d %H:%M:%S", gmtime())
        modified = comment
        # 3. If updated table available, get from WRDS
    if modified == comment and not force and not fpath:
        print(schema + "." + alt_table_name + " already up to date")
        return False
    elif modified == "" and not force:
        print("WRDS flaked out!")
        return False
    else:
        if fpath:
            print("Importing local file.")
        elif force:
            print("Forcing update based on user request.")
        else:
            print("Updated %s.%s is available." % (schema, table_name))
            print("Getting from WRDS.\n")
        wrds_to_pg(table_name=table_name, schema=schema, engine=engine, wrds_id=wrds_id,
                rpath=rpath, fpath=fpath, fix_missing=fix_missing, fix_cr=fix_cr,
                drop=drop, keep=keep, obs=obs, rename=rename, alt_table_name=alt_table_name,
                encoding=encoding, col_types=col_types, create_roles=create_roles,
                sas_schema=sas_schema, sas_encoding=sas_encoding)
        set_table_comment(alt_table_name, schema, modified, engine)
        
        if create_roles:
            if not role_exists(engine, schema):
                create_role(engine, schema)
            
            sql = r"""
                ALTER TABLE "%s"."%s" OWNER TO %s""" % (schema, alt_table_name, schema)
            with engine.connect() as conn:
                conn.execute(text(sql))

            if not role_exists(engine, "%s_access" % schema):
                create_role(engine, "%s_access" % schema)
                
            sql = r"""
                GRANT SELECT ON "%s"."%s"  TO %s_access""" % (schema, alt_table_name, schema)
            res = process_sql(sql, engine)
        else:
            res = True

        return res

def process_sql(sql, engine):

    connection = engine.connect()
    trans = connection.begin()

    try:
        res = connection.execute(text(sql))
        trans.commit()
    except:
        trans.rollback()
        raise

    return True

def run_file_sql(file, engine):
    f = open(file, 'r')
    sql = f.read()
    print("Running SQL in %s" % file)
    
    for i in sql.split(";"):
        j = i.strip()
        if j != "":
            print("\nRunning SQL: %s;" % j)
            process_sql(j, engine)
            
def make_engine(host=None, dbname=None, wrds_id=None):
    if not dbname:
        dbname = getenv("PGDATABASE")
    if not host:
        host = getenv("PGHOST", "localhost")
    if not wrds_id:
        wrds_id = getenv("WRDS_ID")
    
    engine = create_engine("postgresql://" + host + "/" + dbname)
    return engine
  
def role_exists(engine, role):
    with engine.connect() as conn:
        res = conn.execute(text("SELECT COUNT(*) FROM pg_roles WHERE rolname='%s'" % role))
        rs = [r[0] for r in res]
    
    return rs[0] > 0

def create_role(engine, role):
    process_sql("CREATE ROLE %s" % role, engine)
    return True

def get_wrds_tables(schema, wrds_id=None):

    from sqlalchemy import MetaData

    if not wrds_id:
        wrds_id = getenv("WRDS_ID")

    wrds_engine = create_engine("postgresql://%s@wrds-pgdata.wharton.upenn.edu:9737/wrds" % wrds_id,
                                connect_args = {'sslmode':'require'})

    metadata = MetaData(wrds_engine, schema=schema)
    metadata.reflect(schema=schema, autoload=True)
  
    table_list = [key.name for key in metadata.tables.values()]
    wrds_engine.dispose()
    return table_list

def get_contents(table_name, schema, wrds_id=None):
        
    sas_code = make_sas_code(table_name=table_name, \
                             schema=schema, wrds_id=wrds_id)
    
    # Run the SAS code on the WRDS server and get the result
    df = sas_to_pandas(sas_code, wrds_id)
    df['postgres_type'] = df.apply(code_row, axis=1)
    df['name'] = [name.lower() for name in df['name']]
    return df

def pq_types(table_name, sas_schema, wrds_id, col_types):
    # Get names and data types
    df_info = get_contents(table_name, sas_schema, wrds_id)
    names = [name for name in df_info['name']]
    dtypes = dict(zip(df_info['name'], df_info['postgres_type']))
    if col_types:
        for key in col_types.keys():
            dtypes[key] = col_types[key]
    return {'dtypes': dtypes, 'names': names}

def get_pq_file(table_name, schema, data_dir=os.getenv("DATA_DIR"), 
                sas_schema=None, alt_table_name=None):
    data_dir = os.path.expanduser(data_dir)
    
    if not sas_schema:
        sas_schema = schema
    
    if not alt_table_name:
        alt_table_name = table_name
    
    schema_dir = Path(data_dir, schema)

    if not os.path.exists(schema_dir):
        os.makedirs(schema_dir)
    pq_file = Path(data_dir, schema, table_name).with_suffix('.parquet')
    return pq_file

def wrds_to_parquet(table_name, schema, host=os.getenv("PGHOST"), 
                    dbname=os.getenv("PGDATABASE"),
                    wrds_id=os.getenv("WRDS_ID"), 
                    data_dir=os.getenv("DATA_DIR"),
                    memory_limit = "1GB",
                    fix_missing=False, fix_cr=False, drop="", keep="", 
                    obs="", rename="", alt_table_name=None, encoding="utf-8", 
                    col_types=None, create_roles=True, sas_schema=None, 
                    sas_encoding=None, date_format="%Y%m%d", force=False, fpath=None, rpath=None):
    
    if not sas_schema:
        sas_schema = schema
        
    if not alt_table_name:
        alt_table_name = table_name
    
    pq_file = get_pq_file(table_name=table_name, schema=schema, 
                          data_dir=data_dir, sas_schema=sas_schema, 
                          alt_table_name=alt_table_name)
                
    modified = get_modified_str(table_name=table_name, 
                                sas_schema=sas_schema, wrds_id=wrds_id, 
                                encoding=encoding, rpath=rpath)
    
    pq_modified = get_modified_pq(pq_file)
        
    if modified == pq_modified and not force and not fpath:
        print(schema + "." + alt_table_name + " already up to date")
        return False
    if force:
        print("Forcing update based on user request.")
    else:
        print("Updated %s.%s is available." % (schema, alt_table_name))
        print("Getting from WRDS.\n")
    
    types = pq_types(table_name=table_name, sas_schema=sas_schema, 
                     wrds_id=wrds_id, col_types=col_types)
    names = types['names']
    dtypes = types['dtypes']

    print("Saving data to temporary CSV.")
    csv_file = tempfile.NamedTemporaryFile(suffix = ".csv.gz").name
    wrds_to_csv(table_name, schema, csv_file, 
                wrds_id=wrds_id, 
                fix_missing=fix_missing, 
                fix_cr=fix_cr, 
                drop=drop, keep=keep, 
                obs=obs, rename=rename,
                encoding=encoding, 
                sas_schema=sas_schema, 
                sas_encoding=sas_encoding)
    print("Converting temporary CSV to parquet.")
    csv_to_pq(csv_file, pq_file, names, dtypes, modified, date_format)
    return True

def wrds_update_csv(table_name, schema,  
        data_dir=os.getenv("DATA_DIR"),
        host=os.getenv("PGHOST"), dbname=os.getenv("PGDATABASE"), engine=None, 
        wrds_id=os.getenv("WRDS_ID"), rpath=None, fpath=None, force=False, fix_missing=False, fix_cr=False, drop="", keep="", 
        obs="", rename="", alt_table_name=None, encoding=None, col_types=None, create_roles=True,
        sas_schema=None, sas_encoding=None):
        
    if not encoding:
        encoding = "utf-8"

    if not sas_schema:
        sas_schema = schema
        
    schema_dir = Path(data_dir, schema)
    
    if not os.path.exists(schema_dir):
        os.makedirs(schema_dir)
    
    csv_file = Path(data_dir, schema, table_name).with_suffix('.csv.gz')
    # Extract the date-time part from the modified string
    modified = get_modified_str(table_name, sas_schema, wrds_id, encoding=encoding,rpath=rpath)
    
    
    
    if os.path.exists(csv_file):
        csv_modified = get_modified_csv(csv_file)
    else:
        csv_modified = ""
        
    if modified == csv_modified and not force and not fpath:
        print(schema + "." + table_name + " already up to date")
        return False
    if force:
        print("Forcing update based on user request.")
    else:
        print("Updated %s.%s is available." % (schema, table_name))
        print("Getting from WRDS.\n")
    print("Saving data to " + str(csv_file) + ".")    
    date_time_str = modified.split("Last modified: ")[1]
    # print("last modified:"+ date_time_str)
    
    # Convert date_time_str to a datetime object in UTC time zone
    utc_dt = datetime.strptime(date_time_str, "%m/%d/%Y %H:%M:%S").replace(tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc)
    # Convert to epoch time
    
    # Convert to epoch time
    modified_time =  utc_dt.timestamp()
    
    p = get_wrds_process(table_name=table_name, 
                     schema=sas_schema, wrds_id=wrds_id,
                     drop=drop, keep=keep, fix_cr=fix_cr, 
                     fix_missing=fix_missing, obs=obs, rename=rename,
                     encoding=encoding, sas_encoding=sas_encoding)
    with gzip.GzipFile(csv_file, mode='wb') as f:
        shutil.copyfileobj(p, f)
        
    # Get the current time for access time
    current_time = time.time()    
    # Update the last-modified timestamp of the CSV file
    os.utime(csv_file, (current_time, modified_time))    
    return True

def wrds_to_csv(table_name, schema, csv_file=None, 
                wrds_id=os.getenv("WRDS_ID"),
                data_dir=os.getenv("DATA_DIR"),
                fix_missing=False, fix_cr=False, drop="", keep="", 
                obs="", rename="", encoding="utf-8", 
                sas_schema=None, 
                sas_encoding=None, force=False, fpath=None, rpath=None):
        
    if not sas_schema:
        sas_schema = schema

    p = get_wrds_process(table_name=table_name, 
                         schema=sas_schema, wrds_id=wrds_id,
                         drop=drop, keep=keep, fix_cr=fix_cr, 
                         fix_missing=fix_missing, obs=obs, rename=rename,
                         encoding=encoding, sas_encoding=sas_encoding)
    with gzip.GzipFile(csv_file, mode='wb') as f:
        shutil.copyfileobj(p, f)
        
def get_modified_csv(file_name):
     # Get the last-modified time in seconds since the epoch, convert to date and format as str
    utc_dt=datetime.fromtimestamp(os.path.getmtime(file_name))#utc_timestamp=1685112588
    last_modified = utc_dt.astimezone(ZoneInfo("America/Chicago")).strftime("Last modified: %m/%d/%Y %H:%M:%S")

    # Format the datetime object as per the specified format
    # last_modified = last_modified_datetime.strftime("Last modified: %m/%d/%Y %H:%M:%S")
    
    return last_modified
  
def get_modified_pq(file_name):
    if os.path.exists(file_name):
        md = pq.read_schema(file_name)
        schema_md = md.metadata
        if not schema_md:
            return ''
        if b'last_modified' in schema_md.keys():
            last_modified = schema_md[b'last_modified'].decode('utf-8')
        else:
            last_modified = ''
    else:
        last_modified = ''
    return last_modified

def wrds_csv_to_pq(table_name, schema, csv_file, pq_file, 
                   wrds_id=os.getenv("WRDS_ID"),date_format="%Y%m%d", 
                   row_group_size = 1048576):
    types = pq_types(table_name, sas_schema, wrds_id)
    names = types['names']
    dtypes = types['dtypes']
    csv_to_pq(csv_file, pq_file, names, dtypes, modified, date_format)

def csv_to_pq(csv_file, pq_file, names, dtypes, modified, date_format,
              row_group_size = 1048576):
    with duckdb.connect() as con:
        df = con.from_csv_auto(csv_file,
                               compression = "gzip",
                               date_format = date_format,
                               names = names,
                               header = True,
                               dtype = dtypes)
        df_arrow = df.arrow()
        my_metadata = df_arrow.schema.with_metadata({b'last_modified': modified.encode()})
        to_write = df_arrow.cast(my_metadata)
        pq.write_table(to_write, pq_file, row_group_size = row_group_size)
