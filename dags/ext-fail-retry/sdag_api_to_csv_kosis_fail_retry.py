import logging
import urllib3

from datetime import datetime as dt
from pendulum import datetime, from_format
from airflow.decorators import dag, task, task_group
from util.common_util import CommonUtil
from dto.tn_data_bsc_info import TnDataBscInfo
from dto.th_data_clct_mastr_log import ThDataClctMastrLog
from dto.tn_clct_file_info import TnClctFileInfo
from dto.tc_com_dtl_cd import TcCmmnDtlCd as CONST
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.sftp.operators.sftp import SFTPHook
from sqlalchemy.orm import sessionmaker
from airflow.exceptions import AirflowSkipException
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@dag(
    dag_id="sdag_api_to_csv_kosis_fail_retry",
    schedule="*/30 1-2 28 * *",
    start_date=datetime(2023, 9, 16, tz="Asia/Seoul"),  # UI 에 KST 시간으로 표출하기 위한 tz 설정
    catchup=False,
    # render Jinja template as native Python object
    render_template_as_native_obj=True,
    tags=["api_to_csv_retry", "month", "ext"],
)
def api_to_csv_kosis_fail_retry():

    # PostgresHook 객체 생성
    pg_hook = PostgresHook(postgres_conn_id='gsdpdb_db_conn')

    #sqlalchemy 를 이용한 connection
    engine = pg_hook.get_sqlalchemy_engine()

    # sqlalchey session 생성
    session = sessionmaker(engine, expire_on_commit=False)

    # SFTPHook 객체
    sftp_hook = SFTPHook(ssh_conn_id='ssh_inner_conn')

    @task
    def select_count_fail_log(**kwargs):
        select_log_info_stmt = set_select_stmt("distinct", kwargs, None)
        clct_ymd = []
        try:
            with session.begin() as conn:
                for dict_row in conn.execute(select_log_info_stmt).all():
                    clct_ymd.append(dict_row.clct_ymd)
        except Exception as e:
            logging.info(f"select_count_fail_log Exception::: {e}")
            raise e
        if clct_ymd == []:
            logging.info(f"select_count_fail_log ::: 재수집 대상없음 프로그램 종료")
            raise AirflowSkipException()
        return clct_ymd

    def set_select_stmt(set_stmt, kwargs, clct_ymd):
        """
        재수집 대상 로그 정보 조회
        params: set_stmt
        return: select_log_info_stmt
        """
        distinct_stmt = ""
        log_info_stmt = ""
        run_conf = ""
        if kwargs['dag_run'].conf != {}:
            dtst_cd = kwargs['dag_run'].conf['dtst_cd']
            run_conf = f"AND LOWER(a.dtst_cd) = '{dtst_cd}'"
        
        # 재수집 대상 로그 정보 조회
        if set_stmt == None:
            log_info_stmt = f"""
                        AND b.clct_ymd = '{clct_ymd}'
                        ORDER BY b.clct_log_sn
                        """
        if set_stmt == "distinct":
            distinct_stmt = "DISTINCT ON (b.data_crtr_pnttm)"
        select_log_info_stmt = f'''
                                SELECT {distinct_stmt} b.*
                                FROM tn_data_bsc_info a, th_data_clct_mastr_log b
                                WHERE 1=1
                                    AND a.dtst_cd = b.dtst_cd
                                    AND LOWER(clct_yn) = 'y'
                                    AND LOWER(link_yn) = 'y'
                                    AND LOWER(link_clct_mthd_dtl_cd) = 'open_api'
                                    AND LOWER(link_clct_cycle_cd) = 'month'
                                    AND link_ntwk_otsd_insd_se = '외부'
                                    AND LOWER(pvdr_site_cd) = 'ps00010' -- 국가통계포털
                                    AND LOWER(b.step_se_cd) NOT IN ('{CONST.STEP_FILE_STRGE_SEND}', '{CONST.STEP_DW_LDADNG}') -- 하둡전송단계, DW 적재단계 제외
                                    AND COALESCE(stts_msg, '') != '{CONST.MSG_CLCT_COMP_NO_DATA}' -- 원천데이터 없음 제외
                                    AND NOT (LOWER(b.step_se_cd) = '{CONST.STEP_FILE_INSD_SEND}' AND LOWER(b.stts_cd) = '{CONST.STTS_COMP}') -- 내부파일전송 성공 제외
                                    {run_conf}
                                    {log_info_stmt}
                                '''
        return select_log_info_stmt

    @task_group(group_id='call_url_process')
    def call_url_process(clct_ymd):
        from util.file_util import FileUtil

        @task
        def select_collect_data_fail_info(clct_ymd, **kwargs):
            """
            th_data_clct_mastr_log 테이블에서 재수집 대상 로그 정보 조회, tn_data_bsc_info 테이블에서 재수집 대상 기본 정보 조회
            return: tn_data_bsc_info
            """
            select_log_info_stmt = set_select_stmt(None, kwargs, clct_ymd)
            try:
                collect_data_list = CommonUtil.set_fail_info(session, select_log_info_stmt, kwargs)
            except Exception as e:
                logging.info(f"select_collect_data_fail_info Exception::: {e}")
                raise e
            return collect_data_list

        @task
        def create_directory(clct_ymd, collect_data_list, **kwargs):
            """
            수집 파일 경로 생성
            params: tn_data_bsc_info, th_data_clct_mastr_log, tn_clct_file_info
            return: file_path: tn_clct_file_info 테이블에 저장할 파일 경로
            """
            data_interval_end = dt.strptime(clct_ymd, "%Y%m%d")
            root_collect_file_path = kwargs['var']['value'].root_collect_file_path
            file_path = CommonUtil.create_directory(collect_data_list, session, data_interval_end, root_collect_file_path, "y")
            return file_path
        
        @task
        def call_url(collect_data_list, file_path, **kwargs):
            """
            조건별 URL 설정 및 호출하여 csv 파일 생성
            params: tn_data_bsc_info, th_data_clct_mastr_log, tn_clct_file_info, file_path
            return: success_data_list
            """
            import requests
            import os
            import time
            from util.call_url_util import CallUrlUtil
            from xml_to_dict import XMLtoDict
            
            success_data_list = []
            for collect_data_dict in collect_data_list:
                th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                tn_data_bsc_info = TnDataBscInfo(**collect_data_dict['tn_data_bsc_info'])
                tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                log_full_file_path = collect_data_dict['log_full_file_path']
                root_collect_file_path = kwargs['var']['value'].root_collect_file_path

                dtst_cd = th_data_clct_mastr_log.dtst_cd.lower()
                pvdr_site_cd = tn_data_bsc_info.pvdr_site_cd.lower()
                pvdr_inst_cd = tn_data_bsc_info.pvdr_inst_cd.lower()
                base_url = return_url = tn_data_bsc_info.link_data_clct_url

                # 파라미터 및 파라미터 길이 설정
                data_crtr_pnttm_str = th_data_clct_mastr_log.data_crtr_pnttm
                if len(data_crtr_pnttm_str) == 4:
                    data_crtr_pnttm = from_format(data_crtr_pnttm_str,'YYYY')
                if len(data_crtr_pnttm_str) == 6:
                    data_crtr_pnttm = from_format(data_crtr_pnttm_str,'YYYYMM')
                if len(data_crtr_pnttm_str) == 8:
                    data_crtr_pnttm = from_format(data_crtr_pnttm_str,'YYYYMMDD')
                data_interval_start = data_crtr_pnttm  # 처리 데이터의 시작 날짜 (데이터 기준 시점)
                data_interval_end = from_format(th_data_clct_mastr_log.clct_ymd,'YYYYMMDD')  # 실제 실행하는 날짜를 KST 로 설정
                params_dict, params_len = CallUrlUtil.set_params(tn_data_bsc_info, session, data_interval_start, data_interval_end, kwargs)

                retry_num = 0  # 데이터 없을 시 재시도 횟수
                repeat_num = 1  # 파라미터 길이만큼 반복 호출 횟수
                page_index = 1  # 현재 페이지
                total_page = 1  # 총 페이지 수
                
                header = True   # 파일 헤더 모드
                mode = "w"  # 파일 쓰기 모드 overwrite
                link_file_crt_yn = tn_data_bsc_info.link_file_crt_yn.lower()  # csv 파일 생성 여부
                file_name = tn_clct_file_info.insd_file_nm + "." + tn_clct_file_info.insd_file_extn  # csv 파일명
                source_file_name = tn_clct_file_info.insd_file_nm + "." + tn_data_bsc_info.pvdr_sou_data_pvsn_stle  # 원천 파일명
                full_file_path = root_collect_file_path + file_path
                full_file_name = full_file_path + file_name
                link_file_sprtr = tn_data_bsc_info.link_file_sprtr
                file_size = 0  # 파일 사이즈
                row_count = 0  # 행 개수

                try:
                    # 파라미터 길이만큼 반복 호출
                    while repeat_num <= params_len:
                        
                        # 총 페이지 수만큼 반복 호출
                        while page_index <= total_page:
                            
                            # 파라미터 길이만큼 호출 시 while 종료
                            if repeat_num > params_len:
                                break
                            
                            # 재시도 5회 이상 시
                            if retry_num >= 5:
                                # 파라미터 길이 == 1) whlie 종료
                                if params_len == 1:
                                    repeat_num += 1
                                    break
                                else:  # 파라미터 길이 != 1)
                                    # th_data_clct_contact_fail_hstry_log 에 입력
                                    CallUrlUtil.insert_fail_history_log(th_data_clct_mastr_log, return_url, file_path, session, params_dict['param_list'][repeat_num - 1], page_index)

                                    # 총 페이지 수만큼 덜 돌았을 때
                                    if page_index < total_page:  # 다음 페이지 호출
                                        retry_num = 0
                                        page_index += 1
                                        continue
                                    # 총 페이지 수만큼 다 돌고
                                    elif page_index == total_page:
                                        # 파라미터 길이만큼 덜 돌았을 때
                                        if repeat_num < params_len:
                                            retry_num = 0
                                            page_index = 1
                                            repeat_num += 1
                                            continue
                                        # 파라미터 길이만큼 다 돌았을 때
                                        else:
                                            repeat_num += 1
                                            break

                            # url 설정
                            return_url = f"{base_url}{CallUrlUtil.set_url(dtst_cd, pvdr_site_cd, pvdr_inst_cd, params_dict, repeat_num, page_index)}"
                            
                            # url 호출
                            response = requests.get(return_url, verify= False)                            
                            response_code = response.status_code

                            # url 호출 시 메세지 설정
                            header, mode = CallUrlUtil.get_request_message(retry_num, repeat_num, page_index, return_url, total_page, full_file_name, header, mode)
                            
                            if response_code == 200:
                                if tn_data_bsc_info.pvdr_sou_data_pvsn_stle == "json" and 'OpenAPI_ServiceResponse' not in response.text:  # 공공데이터포털 - HTTP 에러 제외
                                    json_data = response.json()
                                if tn_data_bsc_info.pvdr_sou_data_pvsn_stle == "xml" or 'OpenAPI_ServiceResponse' in response.text:  # 공공데이터포털 - HTTP 에러 시 xml 형태
                                    json_data = XMLtoDict().parse(response.text)

                                CallUrlUtil.create_source_file(json_data, source_file_name, full_file_path, mode)

                                # 공공데이터포털 - HTTP 에러 시
                                if 'OpenAPI_ServiceResponse' in response.text:
                                    retry_num += 1
                                    continue

                                result = CallUrlUtil.read_json(json_data, pvdr_site_cd, pvdr_inst_cd, dtst_cd, tn_data_bsc_info.data_se_col_one)
                                result_json = result['result_json_array']
                                result_size = len(result_json)
                                
                                # 데이터 존재 시
                                if result_size != 0:
                                    retry_num = 0  # 재시도 횟수 초기화
                                    if page_index == 1: # 첫 페이지일 때
                                        # 페이징 계산
                                        total_count = int(result['total_count'])
                                        total_page = CallUrlUtil.get_total_page(total_count, result_size)

                                    row_count = FileUtil.check_csv_length(link_file_sprtr, full_file_name)  # 행 개수 확인
                                    if row_count == 0:
                                        header = True
                                        mode = "w"

                                    # csv 파일 생성
                                    CallUrlUtil.create_csv_file(link_file_sprtr, th_data_clct_mastr_log.data_crtr_pnttm, th_data_clct_mastr_log.clct_log_sn, full_file_path, file_name, result_json, header, mode, page_index)

                                # 데이터 결과 없을 경우
                                else:
                                    # 가변 파라미터 변경 후 재호출
                                    if params_len == 1 and params_dict != {} and retry_num < 4:
                                        params_dict['params'] -= 1  # year -= 1
                                        retry_num += 1
                                        continue

                                row_count = FileUtil.check_csv_length(link_file_sprtr, full_file_name)  # 행 개수 확인
                                if row_count != 0:
                                    logging.info(f"현재까지 파일 내 행 개수: {row_count}")

                                # 총 페이지 수 == 1)
                                if total_page == 1:
                                    repeat_num += 1
                                    break
                                else:
                                    if page_index < total_page:
                                        page_index += 1
                                    elif page_index == total_page:
                                        if params_len == 1:
                                            repeat_num += 1
                                            break
                                        elif params_len != 1:
                                            if repeat_num < params_len:
                                                page_index = 1
                                                repeat_num += 1
                                            else: repeat_num += 1
                                            break

                            else:
                                logging.info(f"call_url response_code::: {response_code}")
                                retry_num += 1
                                time.sleep(5)
                                continue

                    # 파일 사이즈 확인
                    if os.path.exists(full_file_name):
                        file_size = os.path.getsize(full_file_name)
                    logging.info(f"call_url file_name::: {file_name}, file_size::: {file_size}")
                    
                    if row_count == 0:
                        CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_COMP, CONST.MSG_CLCT_COMP_NO_DATA, "y")
                    else:
                        # tn_clct_file_info 수집파일정보
                        tn_clct_file_info = CommonUtil.set_file_info(tn_clct_file_info, th_data_clct_mastr_log, tn_clct_file_info.insd_file_nm, file_path, tn_data_bsc_info.link_file_extn, file_size, None)

                        CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_COMP, CONST.MSG_CLCT_COMP, "y")
                        success_data_list.append({
                            "tn_data_bsc_info" : tn_data_bsc_info.as_dict()
                            , "th_data_clct_mastr_log": th_data_clct_mastr_log.as_dict()
                            , "tn_clct_file_info": tn_clct_file_info.as_dict()
                            , "log_full_file_path" : log_full_file_path
                            })
                        if link_file_crt_yn == "y":
                            CommonUtil.update_file_info_table(session, th_data_clct_mastr_log, tn_clct_file_info, tn_clct_file_info.insd_file_nm, file_path, tn_clct_file_info.insd_file_extn, file_size)
                except Exception as e:
                    logging.info(f"call_url Exception::: {e}")
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_ERROR, CONST.MSG_CLCT_ERROR_CALL, "y")
            return success_data_list
        
        @task
        def encrypt_zip_file(success_data_list, file_path, **kwargs):
            """
            파일 압축 및 암호화
            params: success_data_list, file_path
            return: encrypt_file
            """
            tn_data_bsc_info = TnDataBscInfo(**success_data_list[0]['tn_data_bsc_info'])

            try:
                with session.begin() as conn:
                    for collect_data_dict in success_data_list:
                        th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                        th_data_clct_mastr_log = conn.get(ThDataClctMastrLog, th_data_clct_mastr_log.clct_log_sn)
                        tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                        log_full_file_path = collect_data_dict['log_full_file_path']
                        CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_WORK, CONST.MSG_FILE_INSD_SEND_WORK, "y")
            
                pvdr_site_nm = tn_data_bsc_info.pvdr_site_nm
                link_file_extn = tn_data_bsc_info.link_file_extn
                pvdr_sou_data_pvsn_stle = tn_data_bsc_info.pvdr_sou_data_pvsn_stle
                encrypt_key = kwargs['var']['value'].encrypt_key
                root_collect_file_path = kwargs['var']['value'].root_collect_file_path
                full_file_path = root_collect_file_path + file_path

                FileUtil.zip_file(full_file_path, pvdr_site_nm, link_file_extn, pvdr_sou_data_pvsn_stle)
                encrypt_file = FileUtil.encrypt_file(full_file_path, pvdr_site_nm, encrypt_key, pvdr_sou_data_pvsn_stle)
            except Exception as e:
                for collect_data_dict in success_data_list:
                    th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                    th_data_clct_mastr_log = conn.get(ThDataClctMastrLog, th_data_clct_mastr_log.clct_log_sn)
                    tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                    log_full_file_path = collect_data_dict['log_full_file_path']
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_ERROR, CONST.MSG_FILE_INSD_SEND_ERROR_FILE, "y")
                logging.info(f"encrypt_zip_file Exception::: {e}")
                raise AirflowSkipException()
            return {
                    "file_path" : file_path,
                    "encrypt_file" : encrypt_file
                    }

        @task
        def put_file_sftp(success_data_list, encrypt_file_path, **kwargs):
            """
            원격지 서버로 sftp 파일전송
            params: success_data_list, encrypt_file_path
            """
            file_path = encrypt_file_path['file_path']
            encrypt_file = encrypt_file_path['encrypt_file']
            root_collect_file_path = kwargs['var']['value'].root_collect_file_path
            full_file_path = root_collect_file_path + file_path

            local_filepath = full_file_path + encrypt_file
            remote_filepath = kwargs['var']['value'].final_file_path + file_path

            with session.begin() as conn:
                try:
                    if not sftp_hook.path_exists(remote_filepath):
                        sftp_hook.create_directory(remote_filepath)
                    sftp_hook.store_file(remote_filepath + encrypt_file, local_filepath)
                    for collect_data_dict in success_data_list:
                        th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                        th_data_clct_mastr_log = conn.get(ThDataClctMastrLog, th_data_clct_mastr_log.clct_log_sn)
                        tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                        log_full_file_path = collect_data_dict['log_full_file_path']
                        CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_COMP, CONST.MSG_FILE_INSD_SEND_COMP_EXT, "y")
                except Exception as e:
                    for collect_data_dict in success_data_list:
                        th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                        th_data_clct_mastr_log = conn.get(ThDataClctMastrLog, th_data_clct_mastr_log.clct_log_sn)
                        tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                        log_full_file_path = collect_data_dict['log_full_file_path']
                        CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_ERROR, CONST.MSG_FILE_INSD_SEND_ERROR_TRANS_EXT, "y")
                    logging.info(f"put_file_sftp Exception::: {e}")
                    raise e
        
        collect_data_list = select_collect_data_fail_info(clct_ymd)
        file_path = create_directory(clct_ymd, collect_data_list)
        success_data_list = call_url(collect_data_list, file_path)
        encrypt_file_path = encrypt_zip_file(success_data_list, file_path)
        collect_data_list >> file_path >> success_data_list >> encrypt_file_path >> put_file_sftp(success_data_list, encrypt_file_path)

    clct_ymd = select_count_fail_log()
    call_url_process.expand(clct_ymd = clct_ymd)

dag_object = api_to_csv_kosis_fail_retry()

# only run if the module is the main program
if __name__ == "__main__":
    conn_path = "../connections_minio_pg.yaml"
    # variables_path = "../variables.yaml"
    dtst_cd = ""

    dag_object.test(
        execution_date=datetime(2023,10,1,15,00),
        conn_file_path=conn_path,
        # variable_file_path=variables_path,
        # run_conf={"dtst_cd": dtst_cd},
    )