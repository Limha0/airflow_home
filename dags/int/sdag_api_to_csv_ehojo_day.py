import logging
import urllib3

import json
import logging
import jpype
import os

from pendulum import datetime, from_format
from airflow.decorators import dag, task, task_group
from util.common_util import CommonUtil
from dto.tn_data_bsc_info import TnDataBscInfo
from dto.th_data_clct_mastr_log import ThDataClctMastrLog
from dto.tn_clct_file_info import TnClctFileInfo
from dto.tc_com_dtl_cd import TcCmmnDtlCd as CONST
from airflow.providers.postgres.hooks.postgres import PostgresHook
from sqlalchemy.orm import sessionmaker
from airflow.exceptions import AirflowSkipException
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@dag(
    dag_id="sdag_api_to_csv_ehojo_day",
    schedule="@daily",
    start_date=datetime(2023, 9, 16, tz="Asia/Seoul"),  # UI 에 KST 시간으로 표출하기 위한 tz 설정
    catchup=False,
    # render Jinja template as native Python object
    render_template_as_native_obj=True,
    tags=["api_to_csv", "month", "int"],
)
def api_to_csv_ehojo():

    # PostgresHook 객체 생성
    pg_hook = PostgresHook(postgres_conn_id='gsdpdb_db_conn')

    #sqlalchemy 를 이용한 connection
    engine = pg_hook.get_sqlalchemy_engine()

    # sqlalchey session 생성
    session = sessionmaker(engine, expire_on_commit=False)

    @task
    def insert_collect_data_info(**kwargs):
        """
        tn_data_bsc_info 테이블에서 수집 대상 기본 정보 조회 후 th_data_clct_mastr_log 테이블에 입력
        return: collect_data_list
        """
        # print(str(EncryptionUtils.makeTxId('test')))
        # 수집 대상 기본 정보 조회
        select_bsc_info_stmt = '''
                            SELECT *, (SELECT dtl_cd_nm FROM tc_com_dtl_cd WHERE group_cd = 'pvdr_site_cd' AND pvdr_site_cd = dtl_cd) AS pvdr_site_nm -- test
                            FROM tn_data_bsc_info
                            WHERE 1=1
                                AND LOWER(clct_yn) = 'y'
                                AND LOWER(link_yn) = 'y'
                                AND LOWER(link_clct_mthd_dtl_cd) = 'open_api'
                                AND LOWER(link_clct_cycle_cd) = 'day'
                                AND link_ntwk_otsd_insd_se = '내부'
                                AND LOWER(pvdr_site_cd) = 'ps00029' -- 재정
                                AND LOWER(dtst_cd) != 'data1051'-- 실국코드 제외
                            ORDER BY sn
                            '''
        data_interval_start = kwargs['data_interval_start'].in_timezone("Asia/Seoul")  # 처리 데이터의 시작 날짜 (데이터 기준 시점)
        data_interval_end = kwargs['data_interval_end'].in_timezone("Asia/Seoul")  # 실제 실행하는 날짜를 KST 로 설정
        collect_data_list = CommonUtil.insert_collect_data_info(select_bsc_info_stmt, session, data_interval_start, data_interval_end, kwargs)
        if collect_data_list == []:
            logging.info(f"select_collect_data_fail_info ::: 수집 대상없음 프로그램 종료 - AirflowSkipException 발생 예정")
            raise AirflowSkipException()
        return collect_data_list

    @task
    def create_directory(collect_data_list, **kwargs):
        """
        수집 파일 경로 생성
        params: tn_data_bsc_info, th_data_clct_mastr_log, tn_clct_file_info
        return: file_path: tn_clct_file_info 테이블에 저장할 파일 경로
        """
        data_interval_end = kwargs['data_interval_end'].in_timezone("Asia/Seoul")  # 실제 실행하는 날짜를 KST 로 설정
        #final_file_path = kwargs['var']['value'].root_collect_file_path  # local test
        final_file_path = kwargs['var']['value'].final_file_path
        file_path = CommonUtil.create_directory(collect_data_list, session, data_interval_end, final_file_path, "n")
        return file_path
    
    @task_group(group_id='call_url_process')
    def call_url_process(collect_data_list):
        from util.file_util import FileUtil
        
        @task
        def call_url(collect_data_list, file_path, **kwargs):
            """
            조건별 URL 설정 및 호출하여 csv 파일 생성
            params: tn_data_bsc_info, th_data_clct_mastr_log, tn_clct_file_info, file_path
            return: file_size
            """
            logging.info("== call_url task 시작 ==")  # ✅ 무조건 로그 찍히게 추가
            
            import requests
            import os
            import time
            from util.call_url_util import CallUrlUtil
            from util.ehojo_util import EhojoUtil

            th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_list['th_data_clct_mastr_log'])
            tn_data_bsc_info = TnDataBscInfo(**collect_data_list['tn_data_bsc_info'])
            tn_clct_file_info = TnClctFileInfo(**collect_data_list['tn_clct_file_info'])
            log_full_file_path = collect_data_list['log_full_file_path']
            # final_file_path = kwargs['var']['value'].root_collect_file_path  # local test
            final_file_path = kwargs['var']['value'].final_file_path

            dtst_cd = th_data_clct_mastr_log.dtst_cd.lower()
            pvdr_site_cd = tn_data_bsc_info.pvdr_site_cd.lower()
            pvdr_inst_cd = tn_data_bsc_info.pvdr_inst_cd.lower()
            base_url = tn_data_bsc_info.link_data_clct_url
            interface_id = tn_data_bsc_info.pvdr_data_se_vl_one  # 인터페이스 ID
            api_key_ehojo = kwargs['var']['value'].api_key_ehojo  # api key
            encrypt_key_ehojo = kwargs['var']['value'].encrypt_key_ehojo  # 암복호화 key

            # 파라미터 및 파라미터 길이 설정
            data_interval_start = kwargs['data_interval_start'].in_timezone("Asia/Seoul")  # 처리 데이터의 시작 날짜 (데이터 기준 시점)
            data_interval_end = kwargs['data_interval_end'].in_timezone("Asia/Seoul")  # 실제 실행하는 날짜를 KST 로 설정
            params_dict, params_len = CallUrlUtil.set_params(tn_data_bsc_info, session, data_interval_start, data_interval_end, kwargs)

            retry_num = 0  # 데이터 없을 시 재시도 횟수
            repeat_num = 1  # 파라미터 길이만큼 반복 호출 횟수
            pagd_no = 1  # 현재 페이지
            total_page = 1  # 총 페이지 수
            
            header = True   # 파일 헤더 모드
            mode = "w"  # 파일 쓰기 모드 overwrite
            link_file_crt_yn = tn_data_bsc_info.link_file_crt_yn.lower()  # csv 파일 생성 여부
            file_name = tn_clct_file_info.insd_file_nm + "." + tn_clct_file_info.insd_file_extn  # csv 파일명
            source_file_name = tn_clct_file_info.insd_file_nm + "." + tn_data_bsc_info.pvdr_sou_data_pvsn_stle  # 원천 파일명
            full_file_path = final_file_path + file_path
            full_file_name = full_file_path + file_name
            link_file_sprtr = tn_data_bsc_info.link_file_sprtr
            file_size = 0  # 파일 사이즈
            row_count = 0  # 행 개수

            try:
                # 파라미터 길이만큼 반복 호출
                while repeat_num <= params_len:
                    
                    # 총 페이지 수만큼 반복 호출
                    while pagd_no <= total_page:
                        
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
                                # th_data_clct_call_failr_hist_log 에 입력
                                CallUrlUtil.insert_fail_history_log(th_data_clct_mastr_log, base_url, file_path, session, params_dict['param_list'][repeat_num - 1], pagd_no)

                                # 총 페이지 수만큼 덜 돌았을 때
                                if pagd_no < total_page:  # 다음 페이지 호출
                                    retry_num = 0
                                    pagd_no += 1
                                    continue
                                # 총 페이지 수만큼 다 돌고
                                elif pagd_no == total_page:
                                    # 파라미터 길이만큼 덜 돌았을 때
                                    if repeat_num < params_len:
                                        retry_num = 0
                                        pagd_no = 1
                                        repeat_num += 1
                                        continue
                                    # 파라미터 길이만큼 다 돌았을 때
                                    else:
                                        repeat_num += 1
                                        break

                        # url 설정
                        headers = headers = {
                                "API_KEY": api_key_ehojo
                                ,"Content-Type": "application/jsoncharset=utf-8"
                                }
                        data = EhojoUtil.set_url(dtst_cd, params_dict, repeat_num, pagd_no, interface_id, encrypt_key_ehojo, th_data_clct_mastr_log.data_crtr_pnttm)

                        # url 호출
                        response = requests.post(base_url, headers=headers, data=data)
                        response_code = response.status_code
                        logging.info(f"ehojo_day response___base_url::: {base_url}")
                        logging.info(f"ehojo_day response___headers::: {headers}")
                        logging.info(f"ehojo_day response___data::: {data}")

                        # url 호출 시 메세지 설정
                        header, mode = CallUrlUtil.get_request_message(retry_num, repeat_num, pagd_no, base_url, total_page, full_file_name, header, mode)

                        # 정상 응답
                        if response_code == 200:
                            json_data = response.json()

                            # body 복호화 (외부기관에서 요청 시 필요)
                            json_data = EhojoUtil.decrypt_body(json_data, encrypt_key_ehojo)
                            
                            # 원천 데이터 저장
                            CallUrlUtil.create_source_file(json_data, source_file_name, full_file_path, mode)

                            result = CallUrlUtil.read_json(json_data, pvdr_site_cd, pvdr_inst_cd, dtst_cd, tn_data_bsc_info.data_se_col_one)
                            result_json = result['result_json_array']
                            result_size = len(result_json)
                            
                            # 데이터 존재 시
                            if result_size != 0:
                                retry_num = 0  # 재시도 횟수 초기화
                                if pagd_no == 1: # 첫 페이지일 때
                                    # 페이징 계산
                                    total_count = int(result['total_count'])
                                    total_page = CallUrlUtil.get_total_page(total_count, result_size)

                                row_count = FileUtil.check_csv_length(link_file_sprtr, full_file_name)  # 행 개수 확인
                                if row_count == 0:
                                    header = True
                                    mode = "w"

                                # csv 파일 생성
                                CallUrlUtil.create_csv_file(link_file_sprtr, th_data_clct_mastr_log.data_crtr_pnttm, th_data_clct_mastr_log.clct_log_sn, full_file_path, file_name, result_json, header, mode, pagd_no)

                            row_count = FileUtil.check_csv_length(link_file_sprtr, full_file_name)  # 행 개수 확인
                            if row_count != 0:
                                logging.info(f"현재까지 파일 내 행 개수: {row_count}")

                            # 총 페이지 수 == 1)
                            if total_page == 1:
                                repeat_num += 1
                                break
                            else:
                                if pagd_no < total_page:
                                    pagd_no += 1
                                elif pagd_no == total_page:
                                    if params_len == 1:
                                        repeat_num += 1
                                        break
                                    elif params_len != 1:
                                        if repeat_num < params_len:
                                            pagd_no = 1
                                            repeat_num += 1
                                        else: repeat_num += 1
                                        break
                        # 이상 응답
                        else:
                            logging.info(f"call_url response_code::: {response_code}")
                            retry_num += 1
                            time.sleep(5)
                            continue

                # 파일 사이즈 확인
                if os.path.exists(full_file_name):
                    file_size = os.path.getsize(full_file_name)
                logging.info(f"call_url file_name::: {file_name}, file_size::: {file_size}")

                # 실패 로그 개수 확인
                fail_count = CallUrlUtil.get_fail_data_count(th_data_clct_mastr_log.clct_log_sn, session)
                
                if row_count == 0 and fail_count == 0 and retry_num < 5:
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_COMP, CONST.MSG_CLCT_COMP_NO_DATA, "n")
                    raise AirflowSkipException()
                elif fail_count != 0 or retry_num >= 5:
                    logging.info(f"call_url ::: {CONST.MSG_CLCT_ERROR_CALL}")
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_ERROR, CONST.MSG_CLCT_ERROR_CALL, "n")
                    raise AirflowSkipException()
                else:
                    # tn_clct_file_info 수집파일정보
                    tn_clct_file_info = CommonUtil.set_file_info(TnClctFileInfo(), th_data_clct_mastr_log, tn_clct_file_info.insd_file_nm, file_path, tn_data_bsc_info.link_file_extn, file_size, None)
                    
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_COMP, CONST.MSG_CLCT_COMP, "n")
                    if link_file_crt_yn == "y":
                        CommonUtil.update_file_info_table(session, th_data_clct_mastr_log, tn_clct_file_info, tn_clct_file_info.insd_file_nm, file_path, tn_clct_file_info.insd_file_extn, file_size)
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_COMP, CONST.MSG_FILE_INSD_SEND_COMP_INT, "n")
            except AirflowSkipException as e:
                raise e
            except Exception as e:
                CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_ERROR, CONST.MSG_CLCT_ERROR_CALL, "n")
                logging.info(f"call_url Exception::: {e}")
                raise e
        
        file_path >> call_url(collect_data_list, file_path)
    
    collect_data_list = insert_collect_data_info()
    file_path = create_directory(collect_data_list)
    call_url_process.expand(collect_data_list = collect_data_list)

dag_object = api_to_csv_ehojo()

# only run if the module is the main program
if __name__ == "__main__":
    conn_path = "../connections_minio_pg.yaml"
    # variables_path = "../variables.yaml"
    dtst_cd = ""

    dag_object.test(
        execution_date=datetime(2024,10,14,15,00),
        conn_file_path=conn_path,
        # variable_file_path=variables_path,
        # run_conf={"dtst_cd": dtst_cd},
    )
    